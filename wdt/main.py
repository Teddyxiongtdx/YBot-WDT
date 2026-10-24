import asyncio
import time
import traceback
from collections import deque
from ErisPulse import sdk
from ErisPulse.Core import HttpRequest, client
from ErisPulse.Core.Event import message, command, notice, Event

logger = sdk.logger

# ==============================
# 初始化
# ==============================
ADMINGROUP = "477530013" # 请在此处替换为实际的管理群ID

# ==============================
# 数据持久化加载
# ==============================
feedbacks = {}
feedback_id_counter = 1
user_last_submit = {}

def save_data():
    """将内存数据持久化到 Storage"""
    with sdk.storage.transaction():
        sdk.storage.set("feedbacks", feedbacks)
        sdk.storage.set("feedback_id_counter", feedback_id_counter)
        sdk.storage.set("user_last_submit", user_last_submit)

@sdk.lifecycle.on("core.init.complete")
async def ReactToInitEvent(ep):
    global feedbacks, feedback_id_counter, user_last_submit
    
    # 读回原始数据（此时 feedbacks 的 key 可能是 str）
    raw_feedbacks = sdk.storage.get("feedbacks", {})
    # 统一把 key 转回 int，兼容旧数据（str key）和新数据（int key）
    feedbacks = {int(k): v for k, v in raw_feedbacks.items()}
    feedback_id_counter = sdk.storage.get("feedback_id_counter", 1)
    user_last_submit = sdk.storage.get("user_last_submit", {})

# ==============================
# 收消息 (保留基础监听，如需做其他日志记录可用)
# ==============================
@message.on_group_message()
async def ReactToGroupMsg(ep: Event):
    pass

@message.on_private_message()
async def ReactToUserMsg(ep: Event):
    pass

# ==============================
# 指令处理
# ==============================

@command(["反馈"])
async def ReactToFanKui(ep: Event):
    """提交反馈"""
    global feedback_id_counter, feedbacks
    text = ep.get_text().strip()
    uid = ep.get_user_id()
    gid = ep.get_group_id()

    if not text or len(text)<=4:
        await ep.reply("请输入反馈内容。格式: /反馈 [你的内容]")
        return

    # 1. 限速检查 (60秒1条)
    now = time.time()
    if uid in user_last_submit and now - user_last_submit[uid] < 60:
        await ep.reply("反馈提交过于频繁，请1分钟后再试。")
        return
    user_last_submit[uid] = now

    # # 1. 检查非测试
    # if not (await ep.confirm(
    #     "请确认以下事项：\n"
    #     "1. 你的反馈围绕社区环境/氛围问题/社区矛盾等与社区有关的主题。我们不受理技术功能问题反馈\n"
    #     "2. 你的反馈真实有效，不是灌水垃圾反馈。你不得测试本功能（如发送test），仅管理员可测试。\n"
    #     "3. 本人承诺：如果本人滥用反馈功能，将自愿接受处分\n"
    #     "请回答：（是/否），回答否将取消反馈"
    # )):
    #     await ep.reply("反馈已取消")
    #     return

    # 2. 记录反馈
    fid = feedback_id_counter
    feedback_id_counter += 1
    origin_type = "group" if gid else "private"
    origin_id = gid if gid else uid

    feedbacks[fid] = {
        "user_id": uid,
        "origin_type": origin_type,
        "origin_id": str(origin_id),
        "content": text[4:],
        "status": "open",
        "time": now
    }

    # 3. 持久化数据
    save_data()

    # 4. 转发到管理群
    try:
        yh = sdk.adapter.get("Yunhu")
        admin_msg = f"收到新反馈 [ID:{fid}]\n来源: {origin_type} {origin_id}\n内容: {text[4:]}"
        await yh.Send.To("group", ADMINGROUP).Text(admin_msg)
    except:
        logger.error("发送到管理群失败，请检查适配器及ADMINGROUP配置")

    await ep.reply(f"反馈已提交，ID为 {fid}。管理员将会尽快处理。")

@command(["反馈列表"])
async def ViewFeedbackList(ep: Event):
    global feedbacks
    """查看反馈列表"""
    now = time.time()
    WEEK = 7 * 24 * 3600   # 一周的秒数

    # ---- 清理已关闭超过一周的反馈 ----
    expired = []
    for fid, info in feedbacks.items():
        if info["status"] != "closed":
            continue
        closed_time = info.get("closed_time")
        if closed_time is None:# 兼容老数据：以前关闭时没记时间，这里补一个当前时间
            expired.append(fid)   # 没有时间的直接当作过期
            continue
        if now - closed_time > WEEK:
            expired.append(fid)

    if expired:
        for fid in expired:
            del feedbacks[fid]
        save_data()   # 清理后持久化
        logger.info(f"已清理 {len(expired)} 条超期反馈: {expired}")

    # ---- 原来展示逻辑 ----
    if not feedbacks:
        await ep.reply("暂无反馈记录")
        return

    msg = "最近10条反馈记录:\n" + "-" * 20 + "\n"
    recent_feedbacks = list(feedbacks.items())[-10:]
    
    for fid, info in recent_feedbacks:
        status_text = " (closed)" if info["status"] == "closed" else ""
        # 内容过长截断
        content_short = info["content"][:20] + "..." if len(info["content"]) > 20 else info["content"]
        msg += f"[{fid}]{status_text} {content_short}\n"

    await ep.reply(msg.strip())

@command(["修改反馈"])
async def ModifyFeedback(ep: Event):
    """修改反馈内容"""
    global feedbacks
    uid = ep.get_user_id()
    parts = ep.get_command_args()

    if len(parts) < 2:
        await ep.reply("格式: /修改反馈 [ID] [新内容]")
        return

    try:
        fid = int(parts[0])
        new_content = parts[1]
    except ValueError:
        await ep.reply("Failed: ID必须为数字")
        return

    if fid not in feedbacks:
        await ep.reply("Failed: 反馈不存在")
        return

    fb = feedbacks[fid]
    
    # 权限校验
    if fb["user_id"] != uid:
        await ep.reply("Failed: 只能修改自己提交的反馈")
        return

    if fb["status"] == "closed":
        await ep.reply("Failed: 该反馈已被管理员关闭，无法修改")
        return

    # 更新数据
    fb["content"] = new_content
    save_data() # 持久化
    
    try:
        yh = sdk.adapter.get("Yunhu")
        await yh.Send.To("group", ADMINGROUP).Text(f"反馈 [ID:{fid}] 已被用户修改\n新内容: {new_content}")
    except:
        logger.error("发送到管理群失败")

    await ep.reply("修改成功，已同步通知管理员。")

@command(["回复反馈"])
async def ReplyFeedback(ep: Event):
    """管理员回复反馈"""
    global feedbacks
    gid = ep.get_group_id()
    if str(gid) != str(ADMINGROUP):
        await ep.reply("该指令仅限在管理群内使用")
        return

    parts = ep.get_command_args()
    logger.debug(parts)

    if len(parts) < 2:
        await ep.reply("格式: /回复反馈 [ID] [回复内容]")
        return

    try:
        fid = int(parts[0])
        reply_content = parts[1]
    except ValueError:
        await ep.reply("Failed: ID必须为数字")
        return

    if fid not in feedbacks:
        await ep.reply("Failed: 反馈不存在")
        return

    fb = feedbacks[fid]
    yh = sdk.adapter.get("Yunhu")

    # 将回复发送给原反馈人
    if fb["origin_type"] == "group":
        await yh.Send.To("group", fb["origin_id"]).Text(f"管理员回复了你的反馈 [ID:{fid}]:\n{reply_content}")
    else:
        await yh.Send.To("user", fb["origin_id"]).Text(f"管理员回复了你的反馈 [ID:{fid}]:\n{reply_content}")

    await ep.reply(f"已成功回复反馈 {fid}")


@command(["关闭反馈"])
async def CloseFeedback(ep: Event):
    """管理员关闭反馈"""
    global feedbacks
    gid = ep.get_group_id()
    if str(gid) != str(ADMINGROUP):
        await ep.reply("该指令仅限在管理群内使用")
        return

    parts = ep.get_command_args()
    logger.debug(parts)

    if len(parts) < 1:
        await ep.reply("格式: /关闭反馈 [ID]")
        return

    try:
        fid = int(parts[0])
    except ValueError:
        await ep.reply("Failed: ID必须为数字")
        return

    if fid not in feedbacks:
        await ep.reply("Failed: 反馈不存在")
        return

    fb = feedbacks[fid]
    if fb["status"] == "closed":
        await ep.reply("Failed: 该反馈已经是关闭状态")
        return

    fb["status"] = "closed"
    fb["closed_time"] = time.time()
    save_data()  # 持久化

    yh = sdk.adapter.get("Yunhu")
    # 通知反馈人已关闭
    if fb["origin_type"] == "group":
        await yh.Send.To("group", fb["origin_id"]).Text(f"你的反馈 [ID:{fid}] 已被管理员关闭。")
    else:
        await yh.Send.To("user", fb["origin_id"]).Text(f"你的反馈 [ID:{fid}] 已被管理员关闭。")

    await ep.reply(f"已关闭反馈 {fid}")

# ==============================
# 启动
# ==============================
async def main():
    await sdk.run(keep_running=True)

if __name__ == "__main__":
    asyncio.run(main())