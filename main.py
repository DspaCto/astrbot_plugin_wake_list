"""管理员唤醒门禁（Admin Wake Gate）

作用：只允许 AstrBot 管理员使用唤醒词唤醒机器人。
唤醒词不是写死的，取自当前会话所属配置文件的 wake_prefix（每个配置文件可以不同）。

四种模式（mode）：
- blacklist        黑名单：名单内的会话/用户限制；其余放行。
- whitelist        白名单：名单内的会话/用户放行；其余限制。
- complex_black    复杂模式-黑名单：群号层黑名单优先（白名单群号失效）；
                    Q 号层白名单优先（白名单用户即使在黑名单群里也放行）。
- complex_white    复杂模式-白名单：反过来——群号层白名单优先（黑名单群号失效）；
                   Q 号层黑名单优先（黑名单用户即使在白名单群里也限制）。

名单格式（一行一条，和 AstrBot 核心白名单一致）：
- <机器人名称>:GroupMessage:<群号>   群条目，作用于该群
- <机器人名称>:FriendMessage:<QQ号>  用户条目，作用于该用户的私聊和他的群内发言
空行和以 # 开头的行忽略。

管理员始终豁免（管理员判定来自 AstrBot 官方 admins_id，插件不保存任何账号）。
@ 机器人和引用机器人不受本插件影响。
"""

from __future__ import annotations

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter

MODE_BLACKLIST = "blacklist"
MODE_WHITELIST = "whitelist"
MODE_COMPLEX_BLACK = "complex_black"
MODE_COMPLEX_WHITE = "complex_white"


@register(
    "astrbot_plugin_wake_list",
    "DspaCto",
    "黑/白名单，优先级低于管理员",
    "1.2.3",
)
class AdminWakeGate(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    # ------------------------------------------------------------------
    # 配置解析
    # ------------------------------------------------------------------
    def _parse_list(self, key: str) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        """把多行配置解析成 (群条目集合, 用户条目集合)，元素为 (平台ID, 会话ID)。"""
        raw = self.config.get(key, "") or ""
        if not isinstance(raw, str):
            raw = "\n".join(str(x) for x in raw)
        groups: set[tuple[str, str]] = set()
        users: set[tuple[str, str]] = set()
        for line in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 2)
            if len(parts) != 3:
                logger.debug(f"[admin-wake-gate] 忽略无法解析的行：{line}")
                continue
            platform, mtype, sid = (p.strip() for p in parts)
            mt = mtype.lower()
            if mt in ("groupmessage", "group"):
                groups.add((platform, sid))
            elif mt in ("friendmessage", "friend", "private"):
                users.add((platform, sid))
            else:
                logger.debug(f"[admin-wake-gate] 忽略未知会话类型：{line}")
        return groups, users

    def _should_restrict(self, event: AstrMessageEvent) -> bool:
        """按当前模式判断这条消息的会话/用户是否要被限制。"""
        mode = str(self.config.get("mode", MODE_BLACKLIST))
        platform = str(event.get_platform_id() or "")
        uid = str(event.get_sender_id() or "")
        gid = str(event.get_group_id() or "")

        bl_groups, bl_users = self._parse_list("blacklist")
        wl_groups, wl_users = self._parse_list("whitelist")

        in_group = bool(gid)
        gb = in_group and (platform, gid) in bl_groups
        gw = in_group and (platform, gid) in wl_groups
        ub = (platform, uid) in bl_users
        uw = (platform, uid) in wl_users

        if mode == MODE_WHITELIST:
            # 白名单：名单内的群或用户放行，其余限制
            return not (gw or uw)
        if mode == MODE_COMPLEX_BLACK:
            # 群号层：黑名单优先（白名单群号失效）；Q 号层：白名单优先
            if uw:
                return False
            if ub:
                return True
            if in_group:
                return gb
            return False
        if mode == MODE_COMPLEX_WHITE:
            # 群号层：白名单优先（黑名单群号失效）；Q 号层：黑名单优先
            if ub:
                return True
            if uw:
                return False
            if in_group:
                return not gw
            return True
        # 默认/黑名单：名单内的群或用户限制，其余放行
        return gb or ub

    # ------------------------------------------------------------------
    # 事件判断
    # ------------------------------------------------------------------
    @staticmethod
    def _raw_text(event: AstrMessageEvent) -> str:
        """取原始消息文本（唤醒检查会把唤醒词从 event.message_str 剥掉）。"""
        raw = getattr(event.message_obj, "message_str", "") or ""
        if raw.strip():
            return raw.strip()
        for seg in event.get_messages():
            if isinstance(seg, Plain):
                return str(seg.text).strip()
        return ""

    def _wake_prefixes(self, event: AstrMessageEvent) -> list[str]:
        """读取该会话所属配置文件的唤醒词列表（wake_prefix）。

        - 配置缺失（取不到）时才回退到 AstrBot 默认 ["/"]；
        - 配置里显式清空（空列表）表示该 bot 没有前缀唤醒词，
          此时返回空列表，本插件也不拦“前缀”类消息。
        """
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin)
        except Exception:
            return ["/"]
        prefixes = cfg.get("wake_prefix", None)
        if prefixes is None:
            return ["/"]
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        return [str(p) for p in prefixes if str(p)]

    def _starts_with_wake_prefix(self, event: AstrMessageEvent) -> bool:
        raw = self._raw_text(event)
        if not raw:
            return False
        return any(raw.startswith(p) for p in self._wake_prefixes(event))

    @staticmethod
    def _is_command_event(event: AstrMessageEvent) -> bool:
        """判断这条消息是否匹配到了指令（而不是普通聊天）。"""
        handlers = event.get_extra("activated_handlers") or []
        for handler in handlers:
            for fl in getattr(handler, "event_filters", []) or []:
                if isinstance(fl, (CommandFilter, CommandGroupFilter)):
                    return True
        return bool(event.get_extra("handlers_parsed_params") or {})

    def _should_block(self, event: AstrMessageEvent) -> bool:
        """统一条件：非管理员 + 当前会话/用户受限 + 原文以唤醒词开头。"""
        if event.is_admin():
            return False
        if not self._should_restrict(event):
            return False
        return self._starts_with_wake_prefix(event)

    # ------------------------------------------------------------------
    # 拦截
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10000)
    async def block_non_admin_command(self, event: AstrMessageEvent):
        """早拦：非管理员的“唤醒词+指令”在指令 handler 执行前停止。

        非指令的“唤醒词+文字”默认放行给 on_message 记录上下文，
        再由 on_llm_request 晚拦；若关闭了“加入上下文”，则在这里直接停止。
        """
        if not self._should_block(event):
            return
        if self._is_command_event(event):
            logger.info(
                f"[admin-wake-gate] 拦截非管理员指令 mode={self.config.get('mode')} "
                f"sender={event.get_sender_id()} umo={event.unified_msg_origin}"
            )
            event.stop_event()
            return
        # 非指令的 "/文字"
        if not bool(self.config.get("blocked_in_context", True)):
            logger.info(
                f"[admin-wake-gate] 拦截非管理员唤醒（不入上下文） "
                f"mode={self.config.get('mode')} sender={event.get_sender_id()} "
                f"umo={event.unified_msg_origin}"
            )
            event.stop_event()
            return
        # 默认：先让 on_message 把它记进群聊上下文，稍后 on_llm_request 拦截

    @filter.on_llm_request(priority=1000)
    async def block_non_admin_llm(self, event: AstrMessageEvent, req):
        """晚拦：非管理员的 "/文字" 在 LLM 请求发出前停止（上下文已记录）。"""
        if not self._should_block(event):
            return
        logger.info(
            f"[admin-wake-gate] 拦截非管理员唤醒 mode={self.config.get('mode')} "
            f"sender={event.get_sender_id()} umo={event.unified_msg_origin}"
        )
        event.stop_event()
