"""Per-QQ prepaid balance for /pdf, /spdf and /md. Stores amounts, never prompts or answers."""
from contextlib import closing
from contextvars import ContextVar
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
import logging
import math
import re
import secrets
import sqlite3
import time
from pathlib import Path

try:
    from .pdf_task_stats import BEIJING_TZ
except ImportError:
    from pdf_task_stats import BEIJING_TZ

MICRO = 10**6  # balances are integer micro-yuan; 1 token at P 元/百万 token costs P micro-yuan
IMAGE_TOKENS = 1000
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
DEFAULT_PRICES = '* 10 30'
UNAVAILABLE = '⚠️ 计费暂不可用，请联系管理员。'

_job = ContextVar('mathsolve_billing_job', default=None)
_log = logging.getLogger('astrbot')


class BillingDenied(RuntimeError):
    pass


def yuan(micro):
    return f'¥{Decimal(micro) / MICRO:.4f}'


def parse_yuan(text):
    """'10' / '-0.5' -> micro-yuan, or None if not a finite amount with at most 6 decimals."""
    try:
        value = Decimal(str(text).strip())
        if not value.is_finite():
            return None
        value *= MICRO
    except InvalidOperation:
        return None
    if value != value.to_integral_value() or abs(value) > 10**15:
        return None
    return int(value)


def parse_prices(text):
    """One 'model input output' per line in 元/百万 token; the '*' row prices every other model."""
    prices = {}
    for line in str(text or '').splitlines():
        parts = line.split()
        if not parts or parts[0].startswith('#'):
            continue
        try:
            name, inp, out = parts
            price = Decimal(inp), Decimal(out)
        except (ValueError, InvalidOperation):
            raise ValueError(f'价目表格式错误: {line.strip()}') from None
        if not all(p.is_finite() and p >= 0 for p in price):
            raise ValueError(f'价目表价格无效: {line.strip()}')
        prices[name] = price
    if '*' not in prices:
        # An unpriced model would otherwise answer for free.
        raise ValueError('价目表缺少 * 默认价')
    return prices


def cost(prices, model, inp, out):
    price_in, price_out = prices.get(model) or prices['*']
    return math.ceil(price_in * inp + price_out * out)


def _ids(value):
    if isinstance(value, str):
        value = re.split(r'[,，;；\s]+', value)
    return {str(x).strip() for x in value or () if str(x).strip()}


class Billing:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS balances(uid TEXT PRIMARY KEY, micro INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS codes(code TEXT PRIMARY KEY, micro INTEGER NOT NULL, created REAL NOT NULL, used_by TEXT, used REAL);
                CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, ts REAL NOT NULL, uid TEXT NOT NULL, kind TEXT NOT NULL,
                    micro INTEGER NOT NULL, model TEXT, input INTEGER, output INTEGER, note TEXT);
                CREATE INDEX IF NOT EXISTS ledger_uid_ts ON ledger(uid, ts);
            ''')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def _add(db, uid, micro, kind, model=None, inp=None, out=None, note=None):
        db.execute('INSERT INTO ledger(ts, uid, kind, micro, model, input, output, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                   (time.time(), uid, kind, micro, model, inp, out, note))
        db.execute('INSERT INTO balances VALUES (?, ?) ON CONFLICT(uid) DO UPDATE SET micro = micro + excluded.micro', (uid, micro))
        return db.execute('SELECT micro FROM balances WHERE uid = ?', (uid,)).fetchone()[0]

    def add(self, uid, micro, kind, model=None, inp=None, out=None, note=None):
        """Apply a signed amount and its ledger row atomically; return the new balance."""
        with closing(self._connect()) as db, db:
            return self._add(db, uid, micro, kind, model, inp, out, note)

    def balance(self, uid):
        with closing(self._connect()) as db:
            row = db.execute('SELECT micro FROM balances WHERE uid = ?', (uid,)).fetchone()
        return row[0] if row else 0

    def spent(self, uid, since=0.0):
        with closing(self._connect()) as db:
            return -db.execute("SELECT COALESCE(SUM(micro), 0) FROM ledger WHERE uid = ? AND kind = 'charge' AND ts >= ?",
                               (uid, since)).fetchone()[0]

    def create_codes(self, micro, count):
        codes = [''.join(secrets.choice(CODE_ALPHABET) for _ in range(16)) for _ in range(count)]
        with closing(self._connect()) as db, db:
            db.executemany('INSERT INTO codes(code, micro, created) VALUES (?, ?, ?)', [(c, micro, time.time()) for c in codes])
        return codes

    def redeem(self, uid, code):
        """Return (credited, new balance), or None for an unknown or already used code."""
        code = re.sub(r'[\s-]', '', str(code)).upper()
        with closing(self._connect()) as db, db:
            # The guarded UPDATE is the single-use check; two redeemers cannot both match.
            if db.execute('UPDATE codes SET used_by = ?, used = ? WHERE code = ? AND used_by IS NULL',
                          (uid, time.time(), code)).rowcount != 1:
                return None
            micro = db.execute('SELECT micro FROM codes WHERE code = ?', (code,)).fetchone()[0]
            return micro, self._add(db, uid, micro, 'redeem', note=code)


class _Job:
    def __init__(self, owner, uid):
        self.owner, self.uid, self.micro, self.balance = owner, uid, 0, None

    def charge(self, model, inp, out, request, response):
        note = None
        if inp is None or out is None:
            note = '估算'
            if inp is None:
                inp = len(str(request.get('prompt') or '')) + IMAGE_TOKENS * len(request.get('image_urls') or [])
            if out is None:
                out = len(str(getattr(response, 'completion_text', '') or ''))
        try:
            micro = cost(parse_prices(self.owner._cfg('billing_prices', DEFAULT_PRICES)), model, inp, out)
            self.balance = self.owner._billing().add(self.uid, -micro, 'charge', model, inp, out, note)
            self.micro += micro
        except Exception as exc:
            # ponytail: a failed write is logged, not retried; reconcile with /加余额 from this line.
            _log.error('[mathsolve] 扣费失败 uid=%s model=%s input=%s output=%s: %s', self.uid, model, inp, out, exc)

    async def notify(self, event):
        threshold = parse_yuan(self.owner._cfg('billing_low_balance_yuan', 1))
        if threshold is None:
            threshold = MICRO
        if not self.micro or self.balance is None or self.balance >= threshold:
            return
        try:
            await event.send(event.plain_result(f'本次消耗 {yuan(self.micro)}，余额 {yuan(self.balance)}，请及时兑换充值'))
        except Exception as exc:
            _log.warning('[mathsolve] 余额提醒发送失败: %s', type(exc).__name__)


def current_job():
    return _job.get()


def billed(fn):
    """Bill every model call made inside one /pdf, /spdf or /md generation to the sender."""
    @wraps(fn)
    async def run(self, problem_text, event, *args, **kwargs):
        if _job.get() is not None:
            return await fn(self, problem_text, event, *args, **kwargs)
        denial = self._billing_denial(event)
        if denial:
            raise BillingDenied(denial)
        uid = self._billing_payer(event)
        if not uid:
            return await fn(self, problem_text, event, *args, **kwargs)
        job = _Job(self, uid)
        token = _job.set(job)
        try:
            return await fn(self, problem_text, event, *args, **kwargs)
        finally:
            _job.reset(token)
            await job.notify(event)
    return run


class BillingMixin:
    def _billing(self):
        store = getattr(self, '_billing_store', None)
        if store is None:
            store = self._billing_store = Billing(Path(self.DATA_DIR) / 'billing.sqlite3')
        return store

    def _billing_payer(self, event):
        """QQ to charge for this event, or '' when it is free."""
        if not self._cfg('billing_enabled', True):
            return ''
        if str(event.get_group_id() or '').strip() not in _ids(self._cfg('billing_group_ids', [])):
            return ''
        uid = str(event.get_sender_id() or '').strip()
        if not uid:
            raise ValueError('计费群中无法识别发送者 QQ')
        return '' if uid in _ids(self._cfg('billing_free_qq', [])) else uid

    def _billing_denial(self, event):
        """User-facing refusal for a billed generation, or '' to allow it."""
        try:
            uid = self._billing_payer(event)
            if not uid:
                return ''
            parse_prices(self._cfg('billing_prices', DEFAULT_PRICES))
            balance = self._billing().balance(uid)
        except Exception as exc:
            _log.error('[mathsolve] 计费不可用: %s', exc)
            return UNAVAILABLE
        if balance > 0:
            return ''
        return f'❌ 余额不足（当前 {yuan(balance)}），请发送 /兑换 <兑换码> 充值后再试。'

    def _billing_balance_text(self, uid):
        uid = str(uid or '').strip()
        if not uid:
            return '无法获取用户 QQ'
        today = datetime.now(BEIJING_TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        try:
            store = self._billing()
            return (f'💰 {uid} 的余额：{yuan(store.balance(uid))}\n'
                    f'今日消费：{yuan(store.spent(uid, today))}\n'
                    f'累计消费：{yuan(store.spent(uid))}')
        except Exception as exc:
            _log.error('[mathsolve] 余额查询失败: %s', exc)
            return UNAVAILABLE

    def _billing_redeem(self, event, code):
        uid = str(event.get_sender_id() or '').strip()
        if not uid or not str(code or '').strip():
            return '用法：/兑换 <兑换码>'
        try:
            result = self._billing().redeem(uid, code)
        except Exception as exc:
            _log.error('[mathsolve] 兑换失败: %s', exc)
            return UNAVAILABLE
        if result is None:
            return '❌ 兑换码无效或已被使用。'
        credited, balance = result
        return f'✅ 兑换成功，到账 {yuan(credited)}，当前余额 {yuan(balance)}。'

    def _billing_create_codes(self, event, amount, count):
        if str(event.get_group_id() or '').strip() in _ids(self._cfg('billing_group_ids', [])):
            return '为防止兑换码泄露，请在私聊或管理群中生成。'
        micro = parse_yuan(amount)
        if micro is None or micro <= 0 or not str(count).isdigit() or not 1 <= int(count) <= 50:
            return '用法：/生成兑换码 <金额元> <数量1-50>，例如 /生成兑换码 10 5'
        try:
            codes = self._billing().create_codes(micro, int(count))
        except Exception as exc:
            _log.error('[mathsolve] 兑换码生成失败: %s', exc)
            return UNAVAILABLE
        return f'✅ 已生成 {len(codes)} 个 {yuan(micro)} 兑换码（每个只能用一次）：\n' + '\n'.join(codes)

    def _billing_admin_add(self, event, uid, amount):
        micro = parse_yuan(amount)
        if not re.fullmatch(r'\d+', str(uid or '').strip()) or not micro:
            return '用法：/加余额 <QQ> <金额元>，负数为扣除，例如 /加余额 123456 -0.5'
        try:
            balance = self._billing().add(str(uid).strip(), micro, 'admin', note=f'by {event.get_sender_id()}')
        except Exception as exc:
            _log.error('[mathsolve] 调整余额失败: %s', exc)
            return UNAVAILABLE
        return f'✅ 已为 {uid} {"增加" if micro > 0 else "扣除"} {yuan(abs(micro))}，当前余额 {yuan(balance)}。'
