import json
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

def normalize_url(url: str) -> str:
    url = url.strip()
    if "/products" in url:
        return url.replace("/products", "/reviews")
    if "/reviews" not in url:
        url = url.rstrip('/') + '/reviews'
    return url

def extract_username(profile_url: str) -> str:
    parsed_path = urlparse(profile_url).path.strip('/').split('/')
    if len(parsed_path) >= 2 and parsed_path[0] == "profile":
        return parsed_path[1]
    return parsed_path[-1]

def main():
    print("=== Парсер Playerok (перехват реального трафика браузера) ===")
    url_input = input("Введите ссылку на профиль: ").strip()
    days_input = input("За сколько дней нужен отчет (например, 2): ")
    try:
        days_limit = int(days_input)
    except ValueError:
        print("Ошибка: нужно ввести число.")
        return

    target_url = normalize_url(url_input)
    username = extract_username(target_url)
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_limit)

    collected_nodes = []
    seen_ids = set()

    def handle_response(response):
        url = response.url
        if "/graphql" not in url:
            return
        qs = parse_qs(urlparse(url).query)
        op = qs.get("operationName", [""])[0]
        if op != "testimonials":
            return
        try:
            if response.status != 200:
                print(f"[DEBUG] testimonials ответ статус {response.status} (не 200, пропускаем)")
                return
            data = response.json()
        except Exception as e:
            print(f"[DEBUG] Не удалось распарсить JSON ответа: {e}")
            return

        if "errors" in data:
            print(f"[DEBUG] GraphQL ошибка в перехваченном ответе: {data['errors']}")
            return

        edges = data.get("data", {}).get("testimonials", {}).get("edges", [])
        for edge in edges:
            node = edge.get("node", edge)
            nid = node.get("id")
            if nid and nid not in seen_ids:
                seen_ids.add(nid)
                collected_nodes.append(node)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="ru-RU"
        )
        page = context.new_page()
        page.on("response", handle_response)

        print("\n[INFO] Открываем страницу профиля...")
        page.goto(target_url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        print(f"[INFO] Отзывов получено после первой загрузки: {len(collected_nodes)}")

        max_scrolls = 40
        stale_rounds = 0
        last_count = len(collected_nodes)

        for i in range(max_scrolls):
            # Проверяем, не ушли ли мы уже за нужную дату (отзывы идут от новых к старым)
            if collected_nodes:
                oldest = collected_nodes[-1]
                created_at_str = oldest.get("createdAt", "")
                if created_at_str:
                    clean = created_at_str.replace("Z", "+00:00")
                    review_date = datetime.fromisoformat(clean)
                    if review_date < cutoff_date:
                        print("[INFO] Достигнут нужный диапазон дат, прекращаем скролл.")
                        break

            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(1200)

            if len(collected_nodes) == last_count:
                stale_rounds += 1
            else:
                stale_rounds = 0
            last_count = len(collected_nodes)

            if stale_rounds >= 5:
                print("[INFO] Новых отзывов больше не подгружается, останавливаемся.")
                break

        browser.close()

    # Подсчёт по товарам с учётом даты
    sales_data = {}
    for node in collected_nodes:
        created_at_str = node.get("createdAt", "")
        if not created_at_str:
            continue
        clean = created_at_str.replace("Z", "+00:00")
        review_date = datetime.fromisoformat(clean)
        if review_date < cutoff_date:
            continue

        item = node.get("item", {}) or node.get("deal", {}).get("item", {})
        item_name = item.get("name", "Неизвестный товар")
        item_slug = item.get("slug", "")
        item_url = f"https://playerok.com/products/{item_slug}" if item_slug else "Ссылка неизвестна"

        if item_name not in sales_data:
            sales_data[item_name] = {"url": item_url, "count": 0}
        sales_data[item_name]["count"] += 1

    print("\n" + "="*50)
    print(f"Отчет за {days_limit} дней")
    print(f"1. {username} ({target_url})")

    if not sales_data:
        print("   Нет продаж за указанный период.")
    else:
        sorted_sales = sorted(sales_data.items(), key=lambda x: x[1]['count'], reverse=True)
        for name, info in sorted_sales:
            print(f"   - {name} ({info['url']}) купили {info['count']} раз")
    print("="*50 + "\n")

if __name__ == "__main__":
    main()