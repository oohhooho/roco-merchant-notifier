import os
import time
import hashlib
import hmac
import base64
import requests
from urllib.parse import quote
import asyncio
from datetime import datetime, timedelta, timezone
from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright

# ================= 1. 配置区域 =================
ROCOM_API_KEY = os.environ.get("ROCOM_API_KEY")
IMGBB_KEY = os.environ.get("IMGBB_KEY")
NOTIFYME_UUID = os.environ.get("NOTIFYME_UUID")
BARK_KEY = os.environ.get("BARK_KEY")
BARK_SERVER = os.environ.get("BARK_SERVER", "https://api.day.app")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN")
WXPUSHER_TOKEN = os.environ.get("WXPUSHER_TOKEN")
WXPUSHER_UIDS = os.environ.get("WXPUSHER_UIDS", "")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
FEISHU_SECRET = os.environ.get("FEISHU_SECRET", "")  # 签名校验密钥
FEISHU_KEYWORDS = os.environ.get("FEISHU_KEYWORDS", "国王球,棱镜球,祝福项坠,炫彩精灵蛋")  # 逗号分隔，如: 精灵,稀有道具
KEYWORDS = [k.strip() for k in FEISHU_KEYWORDS.split(",") if k.strip()]

GAME_API_URL = "https://wegame.shallow.ink/api/v1/games/rocom/merchant/info"
NOTIFYME_SERVER = "https://notifyme-server.wzn556.top/api/send"
ASSETS_DIR = os.path.abspath("assets/yuanxing-shangren")
HTML_TEMPLATE_FILE = "index.html"
TEMP_RENDER_FILE = "temp_render.html"

# ================= 2. 时间与数据处理逻辑 =================

def get_beijing_time():
    """获取精准的北京时间"""
    return datetime.now(timezone(timedelta(hours=8)))

def format_timestamp(ts_ms):
    """格式化时间戳为 HH:mm"""
    if not ts_ms: return "--:--"
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%H:%M")

def get_round_info():
    """计算当前远行商人的轮次与倒计时"""
    now = get_beijing_time()
    start_time = now.replace(hour=8, minute=0, second=0, microsecond=0)
    
    if now < start_time:
        return {"current": "未开放", "total": 4, "countdown": "尚未开市"}
    
    delta_seconds = int((now - start_time).total_seconds())
    round_index = (delta_seconds // (4 * 3600)) + 1
    
    if round_index > 4:
        return {"current": 4, "total": 4, "countdown": "今日已收市"}
    
    round_end = start_time + timedelta(hours=round_index * 4)
    remaining = round_end - now
    hours, rem = divmod(int(remaining.total_seconds()), 3600)
    minutes, _ = divmod(rem, 60)
    
    countdown_str = f"{hours}小时{minutes}分钟" if hours > 0 else f"{minutes}分钟"
    
    return {
        "current": round_index,
        "total": 4,
        "countdown": countdown_str
    }

def process_data_for_template(data):
    if not data: return {}
    
    now_ms = int(get_beijing_time().timestamp() * 1000)
    round_info = get_round_info()
    
    activities = data.get("merchantActivities") or data.get("merchant_activities") or []
    activity = activities[0] if activities else {}
    
    # 获取三种类型的商品
    buckets = [
        ("道具", activity.get("get_props") or []),
        ("额外道具", activity.get("get_extra_props") or []),
        ("精灵", activity.get("get_pets") or []),
    ]

    # 匹配商品元数据字典 (用于获取价格和限购次数)
    random_goods = data.get("random_goods") if isinstance(data.get("random_goods"), list) else []
    goods_meta_by_name = {
        str(item.get("goods_name", "") or item.get("name", "")).strip(): item
        for item in random_goods
        if isinstance(item, dict) and str(item.get("goods_name", "") or item.get("name", "")).strip()
    }

    all_products = []
    active_products = []
    
    for category, items in buckets:
        for item in items:
            if not isinstance(item, dict): continue

            goods_meta = goods_meta_by_name.get(str(item.get("name", "")).strip(), {})
            
            s_time = item.get("start_time")
            e_time = item.get("end_time")

            # 兜底继承大活动时间
            if s_time is None: s_time = activity.get("start_time")
            if e_time is None: e_time = activity.get("end_time")

            start_ms = int(s_time) if s_time else None
            end_ms = int(e_time) if e_time else None

            is_active = True
            if start_ms is not None and end_ms is not None:
                is_active = start_ms <= now_ms < end_ms

            status_label = "当前轮次"
            if start_ms is not None and now_ms < start_ms:
                status_label = "未开始"
            elif end_ms is not None and now_ms >= end_ms:
                status_label = "已结束"

            # 时间标签格式化
            start_str = format_timestamp(start_ms)
            end_str = format_timestamp(end_ms)
            if start_str[:5] == end_str[:5] and start_str != "--:--":
                time_label = f"{start_str} - {end_str[6:]}" if len(end_str) > 6 else f"{start_str} - {end_str}"
            else:
                time_label = f"{start_str} - {end_str}"

            product = {
                "name": item.get("name", "未知商品"),
                "image": item.get("icon_url", ""),
                "time_label": time_label,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "is_active": is_active,
                "status_label": status_label,
                "price": item.get("price") if item.get("price") not in (None, "") else goods_meta.get("price"),
                "buy_limit_num": item.get("buy_limit_num") if item.get("buy_limit_num") not in (None, "") else goods_meta.get("buy_limit_num")
            }
            
            all_products.append(product)
            if is_active:
                active_products.append(product)
                
    # 历史记录分组逻辑
    today = datetime.fromtimestamp(now_ms / 1000, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    grouped = {}
    
    for product in all_products:
        if product["is_active"]: continue
        start_ms = product["start_ms"]
        if not start_ms: continue
        
        start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone(timedelta(hours=8)))
        if start_dt.strftime("%Y-%m-%d") != today: continue

        key = f"{start_ms}-{product['end_ms'] or ''}"
        if key not in grouped:
            grouped[key] = {
                "time_label": product["time_label"] or "--:--",
                "status_label": product["status_label"] or "其他时段",
                "sort": start_ms,
                "products": []
            }
        group = grouped[key]
        names = {p["name"] for p in group["products"]}
        # 每段最多展示5个不重复商品
        if product["name"] not in names and len(group["products"]) < 5:
            group["products"].append(product)

    history_groups = [
        {k: v for k, v in g.items() if k != "sort"}
        for g in sorted(grouped.values(), key=lambda x: x["sort"])
        if g["products"]
    ]
            
    return {
        "title": activity.get("name", "远行商人"),
        "subtitle": activity.get("start_date", "每日 08:00 / 12:00 / 16:00 / 20:00 刷新"),
        "product_count": len(active_products),
        "round_info": round_info,
        "products": active_products,
        "history_groups": history_groups, # 喂入历史商品数据
        
        # 本地资源支持变量
        "_res_path": "",
        "background": "img/bg.C8CUoi7I.jpg",
        "titleIcon": True
    }

# ================= 3. 图像渲染与上传 =================

async def render_to_image(processed_data):
    """渲染 HTML 并精准切割截图"""
    if not processed_data or processed_data["product_count"] == 0:
        print("当前无活跃商品，跳过渲染")
        return None
    
    screenshot_file = "merchant_render.jpg"
    temp_html_path = os.path.join(ASSETS_DIR, TEMP_RENDER_FILE)
    
    try:
        env = Environment(loader=FileSystemLoader(ASSETS_DIR))
        template = env.get_template(HTML_TEMPLATE_FILE)
        rendered_html = template.render(processed_data)
        
        with open(temp_html_path, "w", encoding="utf-8") as f:
            f.write(rendered_html)
            
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            
            # 维持稳定的 900 宽度，完美规避手机端排版错乱
            await page.set_viewport_size({"width": 900, "height": 1600})
            await page.goto(f"file://{temp_html_path}")
            
            # 等待所有图文加载完毕
            await page.evaluate("document.fonts.ready")
            await page.wait_for_load_state("networkidle")
            
            data_region = page.locator('.merchant-page')
            await data_region.screenshot(path=screenshot_file, type="jpeg", quality=90)
            
            await browser.close()
            print(f"✅ 图片渲染成功: {screenshot_file}")
            return screenshot_file
            
    except Exception as e:
        print(f"❌ 渲染图片失败: {e}")
        return None
    finally:
        if os.path.exists(temp_html_path): os.remove(temp_html_path)

async def upload_to_imgbb(image_path):
    """上传到 ImgBB 图床"""
    if not image_path or not IMGBB_KEY: return None
    try:
        with open(image_path, "rb") as f:
            res = requests.post("https://api.imgbb.com/1/upload", data={"key": IMGBB_KEY}, files={"image": f}, timeout=30)
            json_data = res.json()
            if json_data.get("status") == 200:
                print("✅ 图床上传成功")
                return json_data["data"]["url"]
            else:
                print(f"❌ 图床上传失败: {json_data.get('error', {}).get('message')}")
                return None
    except Exception as e:
        print(f"❌ 图床请求异常: {e}")
        return None

# ================= 4. 推送分发 =================

MAX_RETRY = 2
RETRY_INTERVAL = 300  # 5分钟

def _push_notifyme(title, body, markdown, image_url):
    if not NOTIFYME_UUID: return True  # 未配置视为跳过
    payload = {
        "data": {
            "uuid": NOTIFYME_UUID, "ttl": 86400, "priority": "high",
            "data": {
                "title": title, "body": body, "group": "洛克王国", "bigText": True, "record": 1,
                "markdown": f"{markdown}\n\n![render]({image_url})" if image_url else markdown
            }
        }
    }
    try:
        requests.post(NOTIFYME_SERVER, json=payload, timeout=10)
        print("✅ NotifyMe 推送已发送")
        return True
    except Exception as e:
        print(f"❌ NotifyMe 推送异常: {e}")
        return False

def _push_bark(title, body, markdown, image_url):
    if not BARK_KEY: return True
    try:
        resp = requests.post(f"{BARK_SERVER.rstrip('/')}/{BARK_KEY}", data={
            "title": title, "body": body, "group": "洛克王国", "image": image_url, "isArchive": 1
        }, timeout=10)
        json_data = resp.json()
        if json_data.get("code") == 200:
            print("✅ Bark 推送已发送")
            return True
        else:
            print(f"❌ Bark 推送失败: {json_data.get('message')}")
            return False
    except Exception as e:
        print(f"❌ Bark 推送异常: {e}")
        return False

def _push_ntfy(title, body, markdown, image_url):
    if not NTFY_TOPIC: return True
    try:
        headers = {"Title": title, "Priority": "high", "Tags": "shopping_cart"}
        if image_url:
            headers["Attach"] = image_url
        resp = requests.post(
            f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}",
            data=body.encode("utf-8"), headers=headers, timeout=10,
        )
        if resp.status_code == 200:
            print("✅ ntfy 推送已发送")
            return True
        else:
            print(f"❌ ntfy 推送失败: HTTP {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        print(f"❌ ntfy 推送异常: {e}")
        return False

def _push_pushplus(title, body, markdown, image_url):
    if not PUSHPLUS_TOKEN: return True
    try:
        content = markdown
        if image_url:
            content = f"{markdown}\n\n![render]({image_url})"
        resp = requests.post("https://www.pushplus.plus/send", json={
            "token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": "markdown",
        }, timeout=10)
        json_data = resp.json()
        if json_data.get("code") == 200:
            print("✅ PushPlus 推送已发送")
            return True
        else:
            print(f"❌ PushPlus 推送失败: {json_data.get('msg')}")
            return False
    except Exception as e:
        print(f"❌ PushPlus 推送异常: {e}")
        return False

def _push_wxpusher(title, body, markdown, image_url, item_names=None, matched=None):
    if not (WXPUSHER_TOKEN and WXPUSHER_UIDS): return True
    try:
        uids = [uid.strip() for uid in WXPUSHER_UIDS.split(",") if uid.strip()]

        item_names = item_names or []
        matched = matched or []
        if matched:
            summary = f"🔔🔔🔔稀有道具:{'、'.join(matched)}   |   🛒 当前售卖:{'、'.join(item_names)}"[:99]
        else:
            summary = body

        if image_url:
            banner = (
                f'<div style="background:#fff1f0;border-left:4px solid #ff4d4f;'
                f'padding:10px 12px;margin-bottom:10px;border-radius:6px;'
                f'color:#cf1322;font-weight:bold;font-size:15px;">'
                f'🔔🔔🔔 稀有道具刷新：{"、".join(matched)}</div>'
            ) if matched else ""
            content = (
                f'<h3>🛒 商人刷新详情</h3>'
                f'{banner}'
                f'<img src="{image_url}" style="max-width:100%;border-radius:8px;"/>'
            )
            content_type = 3
        else:
            banner = f"> 🔔🔔🔔 **稀有道具刷新：{'、'.join(matched)}**\n\n" if matched else ""
            content = f"{banner}{markdown}"
            content_type = 2
        resp = requests.post("https://wxpusher.zjiecode.com/api/send/message", json={
            "appToken": WXPUSHER_TOKEN, "content": content, "summary": summary,
            "contentType": content_type, "uids": uids,
        }, timeout=10)
        json_data = resp.json()
        if json_data.get("code") == 1000:
            print("✅ WxPusher 推送已发送")
            return True
        else:
            print(f"❌ WxPusher 推送失败: {json_data.get('msg')}")
            return False
    except Exception as e:
        print(f"❌ WxPusher 推送异常: {e}")
        return False

PUSH_CHANNELS = {
    "NotifyMe": _push_notifyme,
    "Bark": _push_bark,
    "ntfy": _push_ntfy,
    "PushPlus": _push_pushplus,
    "WxPusher": _push_wxpusher,
}

def _push_feishu(title, body, markdown, image_url, item_names, matched):
    """飞书推送：仅当商品名匹配关键词时才推送，支持签名校验"""
    if not FEISHU_WEBHOOK: return True
    if not KEYWORDS:
        print("⚠️ 飞书未配置 FEISHU_KEYWORDS，跳过推送")
        return True
    if not matched:
        print(f"⚠️ 飞书：无商品匹配关键词 {KEYWORDS}，跳过推送")
        return True  # 无匹配不算失败

    try:
        matched_str = "、".join(matched)
        content_lines = [{"tag": "text", "text": f"{title}\n{body}\n匹配商品: {matched_str}"}]
        if image_url:
            content_lines.append({"tag": "a", "text": " 查看详情图", "href": image_url})

        payload = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": f"🔔 {title}（匹配: {matched_str}）",
                        "content": [content_lines]
                    }
                }
            }
        }

        # 签名校验
        url = FEISHU_WEBHOOK
        if FEISHU_SECRET:
            secret = FEISHU_SECRET.strip()
            timestamp = str(int(time.time()))
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
            sign = base64.b64encode(hmac_code).decode("utf-8")
            print(f"🔍 飞书签名调试: timestamp={timestamp}, sign={sign}, secret_len={len(secret)}")
            url = f"{url}?timestamp={timestamp}&sign={quote(sign, safe='')}"

        resp = requests.post(url, json=payload, timeout=10)
        json_data = resp.json()
        if json_data.get("StatusCode") == 0 or json_data.get("code") == 0:
            print(f"✅ 飞书推送已发送（匹配商品: {matched_str}）")
            return True
        else:
            print(f"❌ 飞书推送失败: {json_data.get('msg') or json_data}, 响应: {json_data}")
            return False
    except Exception as e:
        print(f"❌ 飞书推送异常: {e}")
        return False

def push_all(title, body, markdown, image_url, item_names=None):
    """执行全通道推送，失败通道5分钟后重试，最多重试2次"""
    item_names = item_names or []
    matched = [name for name in item_names if any(kw in name for kw in KEYWORDS)]

    failed = set(PUSH_CHANNELS.keys()) | {"飞书"}

    for attempt in range(1, MAX_RETRY + 2):  # 首次 + 2次重试
        if not failed:
            break
        if attempt > 1:
            print(f"⏳ 第{attempt - 1}次重试，等待{RETRY_INTERVAL // 60}分钟...")
            time.sleep(RETRY_INTERVAL)

        still_failed = set()
        for name in failed:
            if name == "飞书":
                success = _push_feishu(title, body, markdown, image_url, item_names, matched)
            elif name == "WxPusher":
                success = _push_wxpusher(title, body, markdown, image_url, item_names, matched)
            else:
                success = PUSH_CHANNELS[name](title, body, markdown, image_url)
            if not success:
                still_failed.add(name)

        failed = still_failed

    if failed:
        print(f"⚠️ 以下通道最终推送失败: {', '.join(failed)}")

# ================= 5. 主入口 =================

async def _fetch_data():
    """请求游戏数据，返回 (raw_data, error, is_transient)

    is_transient 为 True 表示属于网络/服务端瞬时错误（5xx、超时、SSL 握手、连接异常等），
    调用方可在等待后重试；False 表示永久性错误（4xx、业务码非 0 等），重试无意义。
    """
    try:
        resp = requests.get(GAME_API_URL, headers={"X-API-Key": ROCOM_API_KEY}, timeout=30)
        resp.raise_for_status()
        json_data = resp.json()
        raw_data = json_data.get("data", {})
        err = None if json_data.get("code") == 0 else json_data.get("message")
        return raw_data, err, False
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else 0
        # 5xx（含 Cloudflare 520-526）、408 请求超时、429 限流 视为可重试
        is_transient = status >= 500 or status in (408, 429)
        return None, f"请求异常: {e}", is_transient
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.SSLError,
            requests.exceptions.ChunkedEncodingError) as e:
        return None, f"请求异常: {e}", True
    except Exception as e:
        return None, f"请求异常: {e}", False

async def _do_push(raw_data, image_url=None):
    """处理数据并推送，返回是否有活跃商品"""
    processed = process_data_for_template(raw_data)
    if processed["product_count"] == 0:
        return False

    item_names = [p["name"] for p in processed["products"]]
    push_body = f"当前售卖: {'、'.join(item_names)}" if item_names else "当前暂无商品"

    if image_url is None:
        local_img = await render_to_image(processed)
        image_url = await upload_to_imgbb(local_img)

    push_all("📢 远行商人已刷新", push_body, "### 🛒 商人刷新详情", image_url, item_names)
    return True

async def main():
    raw_data, err, is_transient = await _fetch_data()

    # 瞬时错误（如 Cloudflare 525、超时、连接失败）等待后重试
    fetch_attempt = 0
    while (err or not raw_data) and is_transient and fetch_attempt < MAX_RETRY:
        fetch_attempt += 1
        print(f"⏳ 数据获取瞬时失败 [{err}]，{RETRY_INTERVAL // 60}分钟后重试（第{fetch_attempt}/{MAX_RETRY}次）...")
        await asyncio.sleep(RETRY_INTERVAL)
        raw_data, err, is_transient = await _fetch_data()

    if err or not raw_data:
        push_all("⚠️ 监控异常", err or "无法获取数据", "无法获取数据", None, [])
        return

    # 首次推送
    has_active = await _do_push(raw_data)

    # 无活跃商品时，5分钟后重试
    if not has_active:
        for attempt in range(1, MAX_RETRY + 1):
            print(f"⏳ 当前无活跃商品，{RETRY_INTERVAL // 60}分钟后重试（第{attempt}次）...")
            await asyncio.sleep(RETRY_INTERVAL)
            raw_data, err, _ = await _fetch_data()
            if err or not raw_data:
                print(f"❌ 重试获取数据失败: {err}")
                continue
            has_active = await _do_push(raw_data)
            if has_active:
                break
        if not has_active:
            print("⚠️ 重试结束，仍无活跃商品")

if __name__ == "__main__":
    asyncio.run(main())
