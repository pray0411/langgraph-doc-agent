"""高危命令的用户授权：两阶段（challenge → confirm）+ 一次性批准。

背景（两轮外部评审，洞是逐轮暴露的）：

- **第一轮**：`run_command(confirmed=True)` 里的 `confirmed` 是模型自填参数，
  后端没有独立验证 —— 模型可以自己"确认"自己。
- **第二轮**：改成"前端弹窗确认后调 `/api/approve` 登记命令哈希"。方向对，
  但这个接口本身成了新洞：**它接受任何客户端提交的裸命令并直接登记为
  "用户已批准"，不要求任何服务端签发的凭据**。更糟的是前端在打开交互终端时
  是**自动**调它的（`await api('/api/approve', {command: cmd})`）——
  于是"用户确认"这道闸在真实使用路径上等于不存在：批准记录总是存在，
  因为它刚刚由同一个前端写进去。`runterm.start()` 里那句
  "仍显式校验批准记录，不因用户点过按钮就放行任意命令"因此永远为真地通过。

现在的模型（两阶段，nonce 由**服务端**签发）：

1. **challenge**：工具层判定某条命令需要确认时调用 `mint(command, reason)`，
   服务端生成随机 nonce 并记录 `nonce -> (command_hash, command, reason, ts)`，
   **此时尚未批准**。nonce 通过结构化事件（SSE `need_confirm`）交给前端展示。
2. **confirm**：前端把"用户看到的那条命令" + nonce 一起回传 `/api/confirm`；
   `confirm()` 校验 nonce 存在、未过期、命令哈希一致，才把它转为"已批准"
   （一次性，消费即删）。

于是 `/api/confirm` **不能凭空授予批准**：没有服务端签发过 nonce 的命令，
无论怎么调都拿不到批准。"自助登记任意命令"这个原语被消掉了。

**残留面（必须说清楚，不要读成"已经证明有人在键盘前"）**：本项目是单用户
本机工具，服务端无法判定"用户是否真的点了确认"——前端脚本仍能自行走完
prepare → confirm。nonce 把门槛从"裸 POST 一条命令即可"抬到"必须先从服务端
拿到针对这条命令的挑战"，并保证**被显示的命令与被批准的命令是同一个哈希**；
它不构成"在场证明"。真正的边界仍是"不要把它暴露给不受信方"
（见 README 安全边界与 API_TOKEN 的强制策略）。
"""
import hashlib
import secrets
import threading
import time

from logging_setup import audit, get_logger

logger = get_logger(__name__)

_APPROVAL_TTL = 300  # 已批准记录的有效期（秒）：批准 5 分钟内有效
_CHALLENGE_TTL = 300  # 待确认挑战的有效期（秒）

_approved: dict[str, float] = {}  # command_hash -> approved_at（待消费的一次性批准）
_challenges: dict[str, dict] = {}  # nonce -> {seq, command_hash, command, reason, ts}
_lock = threading.Lock()
_seq = [0]  # 挑战序号：SSE 侧用它做游标，只推"新出现的"待确认项


def _hash(command: str) -> str:
    """命令摘要（带固定域前缀，避免与其他地方对同一字符串的裸 SHA-256 混用）。"""
    return hashlib.sha256(b"pray-approval:" + command.encode("utf-8")).hexdigest()


def _prune(now: float) -> None:
    """清掉过期条目（调用方必须已持锁）。"""
    for key, ts in list(_approved.items()):
        if now - ts > _APPROVAL_TTL:
            del _approved[key]
    for nonce, rec in list(_challenges.items()):
        if now - rec["ts"] > _CHALLENGE_TTL:
            del _challenges[nonce]


def mint(command: str, reason: str = "") -> str:
    """为一条待确认命令签发 nonce（只有服务端代码能创建挑战）。返回 nonce。

    同一条命令复用未过期的挑战：模型在同一轮里重复调用同一命令时，
    用户不该被弹两次窗，前端也不该收到两条重复的 need_confirm 事件。
    """
    key = _hash(command)
    now = time.time()
    with _lock:
        _prune(now)
        for nonce, rec in _challenges.items():
            if rec["command_hash"] == key:
                rec["ts"] = now
                if reason:
                    rec["reason"] = reason
                return nonce
        nonce = secrets.token_urlsafe(24)
        _seq[0] += 1
        _challenges[nonce] = {
            "seq": _seq[0],
            "command_hash": key,
            "command": command,
            "reason": reason,
            "ts": now,
        }
    logger.info("签发确认挑战 nonce=%s… hash=%s", nonce[:8], key[:12])
    audit("approval_challenge", nonce_prefix=nonce[:8], command_hash=key[:16],
          command_preview=command[:120], detail=reason[:200])
    return nonce


def latest_seq() -> int:
    """当前最大挑战序号（消费方在开始前取一次，作为游标起点）。"""
    with _lock:
        return _seq[0]


def challenges_since(seq: int) -> list[dict]:
    """返回 seq 之后新签发、且仍未批准的挑战（供 SSE 结构化事件推送）。

    返回的是**结构化**字段而不是给人看的字符串：上一版前端靠正则去匹配
    `NEED_CONFIRM ... 高危命令 [...]`，文案一改（本轮就改了）弹窗就静默失效。
    协议不该编码在人类可读文案里。
    """
    now = time.time()
    out: list[dict] = []
    with _lock:
        _prune(now)
        for nonce, rec in _challenges.items():
            if rec["seq"] > seq:
                out.append({
                    "seq": rec["seq"],
                    "nonce": nonce,
                    "command": rec["command"],
                    "reason": rec["reason"],
                })
    out.sort(key=lambda r: r["seq"])
    return out


def confirm(nonce: str, command: str) -> tuple[bool, str]:
    """用户确认：把 challenge 转为一次性批准。返回 (是否成功, 原因)。"""
    if not nonce:
        return False, "缺少 nonce（该命令没有服务端签发的确认挑战）"
    if not command:
        return False, "缺少 command"
    key = _hash(command)
    now = time.time()
    with _lock:
        _prune(now)
        rec = _challenges.get(nonce)
        if rec is None:
            return False, "nonce 无效、已过期或已被使用"
        if rec["command_hash"] != key:
            # 显示给用户的命令与请求批准的命令不是同一条 —— 直接拒绝
            return False, "nonce 与命令不匹配（被确认的命令与展示的命令不一致）"
        del _challenges[nonce]
        _approved[key] = now
    logger.info("用户确认高危命令 hash=%s", key[:12])
    audit("approval_granted", command_hash=key[:16], command_preview=command[:120],
          ttl_s=_APPROVAL_TTL)
    return True, "已确认"


def is_approved(command: str) -> bool:
    """检查命令是否已被用户确认；命中则消费（用后即删）。"""
    key = _hash(command)
    with _lock:
        rec = _approved.get(key)
        if rec is None:
            return False
        # 过期检查
        if time.time() - rec > _APPROVAL_TTL:
            del _approved[key]
            logger.info("批准已过期 hash=%s", key[:12])
            audit("approval_expired", command_hash=key[:16], ttl_s=_APPROVAL_TTL)
            return False
        # 一次性消费：批准只对"这一条命令"有效一次
        del _approved[key]
    audit("approval_consumed", command_hash=key[:16], command_preview=command[:120])
    return True


def clear() -> None:
    with _lock:
        _approved.clear()
        _challenges.clear()


def reset_state() -> None:
    """重置批准表与挑战表（测试复位用）。"""
    with _lock:
        _approved.clear()
        _challenges.clear()
        _seq[0] = 0


def pending_count() -> int:
    """仍有效（未过期、未消费）的批准条数（观测/自检用）。"""
    now = time.time()
    with _lock:
        _prune(now)
        return sum(1 for ts in _approved.values() if now - ts <= _APPROVAL_TTL)


def challenge_count() -> int:
    """仍待确认（已签发未批准、未过期）的挑战条数（观测/自检用）。"""
    now = time.time()
    with _lock:
        _prune(now)
        return len(_challenges)
