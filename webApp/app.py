import json
import queue
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

from flask import Flask, render_template, request, Response, jsonify
from playwright.sync_api import sync_playwright

app = Flask(__name__)

# Инструмент рассчитан на одного пользователя за раз (личный тул продавца),
# поэтому состояние храним просто в памяти процесса.
state_lock = threading.Lock()
is_running = False
event_queue: "queue.Queue" = queue.Queue()


def normalize_url(url: str) -> str:
    url = url.strip()
    if "/products" in url:
        return url.replace("/products", "/reviews")
    if "/reviews" not in url:
        url = url.rstrip("/") + "/reviews"
    return url


def extract_username(profile_url: str) -> str:
    parsed_path = urlparse(profile_url).path.strip("/").split("/")
    if len(parsed_path) >= 2 and parsed_path[0] == "profile":
        return parsed_path[1]
    return parsed_path[-1]


def emit(event_type: str, **payload):
    event_queue.put({"type": event_type, **payload})


def scan_one_profile(url_input: str, cutoff_date, seller_index: int, seller_total: int):
    """Скраппит один профиль и возвращает отчёт по нему.
    Логика перехвата/скролла — без изменений от исходного скрипта."""
    target_url = normalize_url(url_input)
    username = extract_username(target_url)

    emit("seller_start", index=seller_index, total=seller_total, username=username, target_url=target_url)
    emit("log", seller=username, level="info",
         message=f"[{seller_index}/{seller_total}] Открываем профиль: {target_url}")

    collected_nodes = []
    seen_ids = set()

    def handle_response(response):
        resp_url = response.url
        if "/graphql" not in resp_url:
            return
        qs = parse_qs(urlparse(resp_url).query)
        op = qs.get("operationName", [""])[0]
        if op != "testimonials":
            return
        try:
            if response.status != 200:
                emit("log", seller=username, level="warn", message=f"testimonials: статус {response.status}, пропускаем")
                return
            data = response.json()
        except Exception as e:
            emit("log", seller=username, level="warn", message=f"Не удалось распарсить JSON ответа: {e}")
            return

        if "errors" in data:
            emit("log", seller=username, level="warn", message=f"GraphQL ошибка: {data['errors']}")
            return

        edges = data.get("data", {}).get("testimonials", {}).get("edges", [])
        new_count = 0
        for edge in edges:
            node = edge.get("node", edge)
            nid = node.get("id")
            if nid and nid not in seen_ids:
                seen_ids.add(nid)
                collected_nodes.append(node)
                new_count += 1
        if new_count:
            emit("log", seller=username, level="hit",
                 message=f"Поймано {new_count} новых отзывов (всего захвачено: {len(collected_nodes)})")
            emit("progress", seller=username, collected=len(collected_nodes))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )
        page = context.new_page()
        page.on("response", handle_response)

        emit("log", seller=username, level="info", message="Загружаем страницу...")
        page.goto(target_url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        emit("log", seller=username, level="info", message=f"Отзывов после первой загрузки: {len(collected_nodes)}")

        max_scrolls = 40
        stale_rounds = 0
        last_count = len(collected_nodes)

        for _ in range(max_scrolls):
            if collected_nodes:
                oldest = collected_nodes[-1]
                created_at_str = oldest.get("createdAt", "")
                if created_at_str:
                    clean = created_at_str.replace("Z", "+00:00")
                    review_date = datetime.fromisoformat(clean)
                    if review_date < cutoff_date:
                        emit("log", seller=username, level="info",
                             message="Достигнут нужный диапазон дат, останавливаем скролл.")
                        break

            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1200)

            if len(collected_nodes) == last_count:
                stale_rounds += 1
            else:
                stale_rounds = 0
            last_count = len(collected_nodes)

            if stale_rounds >= 5:
                emit("log", seller=username, level="info", message="Новые отзывы больше не подгружаются, останавливаемся.")
                break

        browser.close()

    # Подсчёт продаж по товарам — считаем только отзывы, реально попавшие в диапазон дат.
    # Раньше в качестве "всего отзывов" ошибочно показывался общий захват (collected_nodes),
    # включавший записи за пределами cutoff_date, из-за чего число не совпадало с суммой по товарам.
    sales_data = {}
    filtered_count = 0
    for node in collected_nodes:
        created_at_str = node.get("createdAt", "")
        if not created_at_str:
            continue
        clean = created_at_str.replace("Z", "+00:00")
        review_date = datetime.fromisoformat(clean)
        if review_date < cutoff_date:
            continue

        filtered_count += 1
        item = node.get("item", {}) or node.get("deal", {}).get("item", {})
        item_name = item.get("name", "Неизвестный товар")
        item_slug = item.get("slug", "")
        item_url = f"https://playerok.com/products/{item_slug}" if item_slug else ""

        if item_name not in sales_data:
            sales_data[item_name] = {"url": item_url, "count": 0}
        sales_data[item_name]["count"] += 1

    sorted_sales = sorted(sales_data.items(), key=lambda x: x[1]["count"], reverse=True)
    results = [{"name": name, "url": info["url"], "count": info["count"]} for name, info in sorted_sales]

    report = {
        "username": username,
        "target_url": target_url,
        "total_reviews": filtered_count,
        "results": results,
    }
    emit("seller_done", **report)
    return report


def run_scraper(url_inputs: list, days_input: str):
    global is_running
    try:
        try:
            days_limit = int(days_input)
        except ValueError:
            emit("error", message="Нужно ввести целое число дней.")
            return

        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_limit)
        seller_total = len(url_inputs)
        sellers_reports = []

        for idx, url_input in enumerate(url_inputs, start=1):
            try:
                report = scan_one_profile(url_input, cutoff_date, idx, seller_total)
                sellers_reports.append(report)
            except Exception as e:
                emit("log", seller=url_input, level="error", message=f"Не удалось обработать {url_input}: {e}")

        # Общий отчёт по всем продавцам сразу — товары агрегируются по названию.
        combined = {}
        for sr in sellers_reports:
            for r in sr["results"]:
                key = r["name"]
                if key not in combined:
                    combined[key] = {"url": r["url"], "count": 0}
                combined[key]["count"] += r["count"]
        combined_sorted = sorted(combined.items(), key=lambda x: x[1]["count"], reverse=True)
        combined_results = [{"name": n, "url": i["url"], "count": i["count"]} for n, i in combined_sorted]

        emit(
            "done",
            days=days_limit,
            sellers=sellers_reports,
            combined=combined_results,
            total_reviews=sum(sr["total_reviews"] for sr in sellers_reports),
        )

    except Exception as e:
        emit("error", message=f"Ошибка: {e}")
    finally:
        with state_lock:
            is_running = False


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start", methods=["POST"])
def start():
    global is_running
    data = request.get_json(force=True, silent=True) or {}
    urls_raw = data.get("urls") or ""
    days_input = str(data.get("days") or "").strip()

    url_inputs = []
    for line in urls_raw.splitlines():
        line = line.strip()
        if line and line not in url_inputs:
            url_inputs.append(line)

    if not url_inputs:
        return jsonify({"ok": False, "error": "Укажите хотя бы одну ссылку на профиль."}), 400

    with state_lock:
        if is_running:
            return jsonify({"ok": False, "error": "Уже выполняется другой запрос, дождитесь завершения."}), 409
        is_running = True

    # чистим очередь от событий прошлого запуска
    while not event_queue.empty():
        try:
            event_queue.get_nowait()
        except queue.Empty:
            break

    thread = threading.Thread(target=run_scraper, args=(url_inputs, days_input), daemon=True)
    thread.start()
    return jsonify({"ok": True})


@app.route("/api/stream")
def stream():
    def gen():
        while True:
            event = event_queue.get()
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event["type"] in ("done", "error"):
                break

    return Response(gen(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)