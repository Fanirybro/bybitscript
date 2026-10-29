"""
Script d'interception réseau pour capturer le format exact
des requêtes envoyées par Bybit lors de l'inscription.

Ce script lance Chrome, vous laisse effectuer UNE inscription manuellement,
et capture toutes les requêtes réseau pour comprendre l'API.
"""
import asyncio
import json
import os
import sys
from playwright.async_api import async_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USER_DATA_DIR = os.path.join(BASE_DIR, "chrome_profile")
LOG_FILE = os.path.join(BASE_DIR, "captured_requests.json")

# Mots-clés pour filtrer les requêtes intéressantes
INTERESTING_KEYWORDS = [
    "register", "captcha", "geetest", "verify", "otp",
    "sms", "send_code", "security", "risk", "mobile",
    "check", "country"
]

captured = []

async def main():
    print("=" * 60)
    print("  INTERCEPTEUR DE REQUÊTES RÉSEAU BYBIT")
    print("  -> Effectuez UNE inscription manuellement dans le navigateur")
    print("  -> Le script capture TOUTES les requêtes API")
    print("  -> Appuyez sur Ctrl+C dans le terminal quand c'est terminé")
    print("=" * 60)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=False,
            channel="chrome",
            ignore_default_args=["--no-sandbox", "--enable-automation"],
            args=[
                "--start-maximized",
                "--test-type",
                "--disable-blink-features=AutomationControlled",
            ]
        )

        page = context.pages[0] if context.pages else await context.new_page()

        async def capture_request(request):
            url = request.url.lower()
            if any(kw in url for kw in INTERESTING_KEYWORDS):
                headers = dict(request.headers)
                post_data = request.post_data
                entry = {
                    "method": request.method,
                    "url": request.url,
                    "headers": headers,
                    "post_data": post_data,
                    "resource_type": request.resource_type,
                }
                captured.append(entry)
                print(f"\n  [📡 CAPTURÉ] {request.method} {request.url}")
                if post_data:
                    try:
                        parsed = json.loads(post_data)
                        print(f"  [BODY] {json.dumps(parsed, indent=2, ensure_ascii=False)[:500]}")
                    except Exception:
                        print(f"  [BODY] {post_data[:300]}")

        async def capture_response(response):
            url = response.url.lower()
            if any(kw in url for kw in INTERESTING_KEYWORDS):
                status = response.status
                try:
                    body = await response.text()
                    try:
                        parsed = json.loads(body)
                        body_preview = json.dumps(parsed, indent=2, ensure_ascii=False)[:500]
                    except Exception:
                        body_preview = body[:500]
                except Exception:
                    body_preview = "(inaccessible)"

                # Ajouter la réponse à la dernière entrée correspondante
                for entry in reversed(captured):
                    if response.url in entry["url"]:
                        entry["response_status"] = status
                        entry["response_body"] = body_preview
                        break

                print(f"\n  [📥 RÉPONSE] HTTP {status} {response.url}")
                print(f"  [DATA] {body_preview}")

        page.on("request", capture_request)
        page.on("response", lambda r: asyncio.create_task(capture_response(r)))

        await page.goto("https://www.bybit.com/en/register", wait_until="domcontentloaded")
        print("\n  [✓] Page Bybit ouverte. Effectuez l'inscription manuellement.")
        print("  [*] Toutes les requêtes API sont capturées en temps réel...\n")

        # Attente infinie jusqu'à ce que l'utilisateur ferme ou Ctrl+C
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            # Sauvegarde de toutes les requêtes capturées
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(captured, f, indent=2, ensure_ascii=False)
            print(f"\n  [💾] {len(captured)} requêtes capturées et sauvegardées dans :")
            print(f"       {LOG_FILE}")
            await context.close()

if __name__ == "__main__":
    asyncio.run(main())

