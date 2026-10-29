import asyncio
import json
import os
import random
import re
import sys
import urllib.request
from playwright.async_api import async_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(BASE_DIR, "GeekedTest"))
try:
    from geeked.slide import SlideSolver
except Exception:
    SlideSolver = None

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ──────────────────────────────────────────────
#  Configuration & Réglages de performance
# ──────────────────────────────────────────────
TARGET_URL = "https://www.bybit.com/en/register"
CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER_DATA_DIR = os.path.join(BASE_DIR, "chrome_profile")

# Fichiers de données
CSV_FILE = os.path.join(BASE_DIR, "Algeria new 5K.csv")
PROCESSED_FILE = os.path.join(BASE_DIR, "success_numbers.txt")

# Options d'optimisation
HEADLESS = False               # Mettre à True si vous ne souhaitez pas afficher la fenêtre (plus rapide)
BLOCK_USELESS_RESOURCES = False # Laisser à False pour éviter que Geetest ne bloque avec "Captcha Forbidden (60500)"
SKIP_ON_CAPTCHA = False         # Mettre à True pour passer directement au numéro suivant si Geetest apparaît
CAPTCHA_TIMEOUT = 25           # Temps max (s) accordé si un Captcha apparaît (résolution manuelle)
OTP_PAGE_TIMEOUT = 18          # Temps max (s) d'attente pour l'affichage complet de la page de saisie d'OTP
DELAY_BETWEEN_NUMBERS = 2.5    # Délai minimal (s) entre deux itérations (évite le rate-limit Akamai)

# ──────────────────────────────────────────────
#  Juridictions & Pays restreints par Bybit
# ──────────────────────────────────────────────
BYBIT_RESTRICTED_COUNTRIES = {
    "US": "États-Unis",
    "CN": "Chine",
    "HK": "Hong Kong",
    "SG": "Singapour",
    "CA": "Canada",
    "KP": "Corée du Nord",
    "CU": "Cuba",
    "IR": "Iran",
    "SY": "Syrie",
    "SD": "Soudan",
    "UZ": "Ouzbékistan",
    "FR": "France",
    "GB": "Royaume-Uni",
    "NL": "Pays-Bas",
    "AF": "Afghanistan",
    "BY": "Biélorussie",
    "MM": "Birmanie (Myanmar)",
    "LX": "Luxembourg",
}

def verify_ip_country() -> tuple[bool, str, str, str]:
    """
    Vérifie l'adresse IP publique et détecte si elle provient d'un pays restreint par Bybit.
    Renvoie: (is_eligible, ip_address, country_name, country_code)
    """
    endpoints = [
        ("http://ip-api.com/json", lambda d: (d.get("query", ""), d.get("country", ""), d.get("countryCode", "").upper())),
        ("https://api.country.is", lambda d: (d.get("ip", ""), d.get("country", ""), d.get("country", "").upper())),
    ]

    for url, extractor in endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                ip, country, code = extractor(data)
                if code:
                    is_restricted = code in BYBIT_RESTRICTED_COUNTRIES
                    return (not is_restricted), ip, country, code
        except Exception:
            continue

    return True, "Indéterminée", "Indéterminé", "??"

# ──────────────────────────────────────────────
#  Gestion des numéros & Reprise aléatoire
# ──────────────────────────────────────────────
def load_algeria_numbers() -> list[str]:
    """Charge et nettoie les numéros algériens uniques depuis le fichier CSV."""
    numbers = []
    if not os.path.exists(CSV_FILE):
        print(f"[!] Fichier CSV introuvable : {CSV_FILE}")
        return numbers

    with open(CSV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            # Ignorer en-têtes et lignes non numériques
            cleaned = "".join(c for c in raw if c.isdigit() or c == "+")
            if not cleaned or not any(c.isdigit() for c in cleaned):
                continue

            num = cleaned
            if num.startswith("+213"):
                num = num[4:]
            elif num.startswith("00213"):
                num = num[5:]
            elif num.startswith("213") and len(num) > 9:
                num = num[3:]
            elif num.startswith("0") and len(num) == 10:
                num = num[1:]

            if num and num not in numbers:
                numbers.append(num)

    return numbers

def get_random_unprocessed_queue(all_numbers: list[str]) -> tuple[list[str], int]:
    """
    Lit success_numbers.txt pour exclure tous les numéros déjà traités avec succès,
    puis mélange aléatoirement les numéros restants.
    """
    processed_set = set()
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
            for line in f:
                n = line.strip()
                if n:
                    processed_set.add(n)

    # Filtrer uniquement les numéros qui ne figurent pas dans success_numbers.txt
    remaining = [n for n in all_numbers if n not in processed_set]

    # Mélanger aléatoirement la liste restante
    random.shuffle(remaining)

    return remaining, len(processed_set)

def mark_number_success(number: str):
    """Enregistre immédiatement le numéro réussi dans le fichier de marquage."""
    with open(PROCESSED_FILE, "a", encoding="utf-8") as f:
        f.write(f"{number}\n")
        f.flush()

# ──────────────────────────────────────────────
#  Surveillance & Diagnostics du navigateur
# ──────────────────────────────────────────────
browser_console_logs = []
network_errors = []

def reset_chrome_crash_state(user_data_dir: str):
    """Réinitialise l'état de crash de Chrome pour éliminer la bulle 'Restaurer les pages ?'."""
    prefs_file = os.path.join(user_data_dir, "Default", "Preferences")
    if os.path.exists(prefs_file):
        try:
            with open(prefs_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "profile" in data:
                data["profile"]["exit_type"] = "Normal"
                data["profile"]["exited_cleanly"] = True
            with open(prefs_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

def setup_browser_diagnostics(page):
    """Attache des écouteurs pour capturer les erreurs réseau (API) et console."""
    def on_console(msg):
        text = msg.text
        # Ignorer les bruits et avertissements de routine (React, Firebase, polices...)
        if any(ignore in text for ignore in [
            "FedCM", "Inter-latin", "preload", "favicon", "GSI_LOGGER",
            "WebPush", "FIREBASE", "FIREBASE_CONFIG", "languageModal"
        ]):
            return
        if msg.type in ["error", "warning"]:
            browser_console_logs.append(f"[{msg.type.upper()}] {text}")

    async def on_response(response):
        if response.status >= 400:
            url = response.url
            if any(k in url for k in ["bybit", "geetest", "akamai"]):
                endpoint = url.split("?")[0]
                try:
                    body = await response.text()
                    preview = body[:250].replace("\n", " ").strip() if body else "Corps vide"
                    network_errors.append(f"HTTP {response.status} sur {endpoint} -> {preview}")
                except Exception:
                    network_errors.append(f"HTTP {response.status} sur {endpoint}")

    page.on("console", on_console)
    page.on("response", lambda r: asyncio.create_task(on_response(r)))

def print_browser_diagnostics():
    """Affiche un résumé clair des requêtes rejetées et des erreurs console."""
    if not network_errors and not browser_console_logs:
        return

    print("\n  🔍 [DIAGNOSTIC NAVIGATEUR EN DIRECT]")
    if network_errors:
        print("  ── Requêtes serveur/API échouées :")
        for item in network_errors[-4:]:
            print(f"     • {item}")
    if browser_console_logs:
        print("  ── Erreurs console JavaScript :")
        for item in browser_console_logs[-4:]:
            print(f"     • {item}")
    print("  " + "─" * 52)

# ──────────────────────────────────────────────
#  Accélération réseau : Blocage des requêtes inutiles
# ──────────────────────────────────────────────
async def setup_route_blocking(page):
    """Intercepte et bloque les requêtes de tracking, pubs et médias lourds."""
    blocked_domains = [
        "google-analytics.com",
        "googletagmanager.com",
        "datadoghq.com",
        "sentry.io",
        "onetrust.com",
        "braze.com",
        "branch.io",
        "hotjar.com",
        "facebook.net",
        "doubleclick.net",
        "adjust.com",
        "appsflyer.com",
    ]

    async def route_handler(route):
        url = route.request.url.lower()
        res_type = route.request.resource_type

        # 1. Bloquer les trackers & télémétrie connus
        if any(d in url for d in blocked_domains):
            await route.abort()
            return

        # 2. Bloquer les médias et polices inutiles
        if res_type in ["media", "font"]:
            await route.abort()
            return

        # 3. Bloquer les images superflues (en préservant Geetest et les icônes UI)
        if res_type == "image":
            if "geetest" in url or "sms" in url or "captcha" in url or "bybit" in url:
                await route.continue_()
            else:
                await route.abort()
            return

        await route.continue_()

    await page.route("**/*", route_handler)

# ──────────────────────────────────────────────
#  Auto-Solveur OpenCV pour GeeTest v4 Slider
# ──────────────────────────────────────────────
async def extract_image_bytes(locator) -> bytes:
    """Récupère les octets d'une image GeeTest (via background-image, style ou capture)."""
    # 1. Vérifier dans le style calculé
    try:
        bg = await locator.evaluate("el => window.getComputedStyle(el).backgroundImage")
        m = re.search(r"https?://[^\s\"\'&)]+\.png", bg or "")
        if m:
            req = urllib.request.Request(m.group(0), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.read()
    except Exception:
        pass

    # 2. Vérifier dans l'attribut style inline du HTML (ex: style="background-image: url(...)")
    try:
        style = await locator.get_attribute("style")
        m = re.search(r"https?://[^\s\"\'&)]+\.png", style or "")
        if m:
            req = urllib.request.Request(m.group(0), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.read()
    except Exception:
        pass

    # 3. Fallback capture directe
    return await locator.screenshot()

async def auto_solve_geetest_slider(page) -> bool:
    """Résout automatiquement le puzzle coulissant GeeTest v4 avec OpenCV."""
    if SlideSolver is None:
        print("  [!] Module SlideSolver indisponible.")
        return False

    try:
        # Ciblage précis des éléments VISIBLES dans la fenêtre active de GeeTest
        bg_el = page.locator(".geetest_window:visible .geetest_bg, .geetest_slide:visible .geetest_bg, .geetest_bg:visible").first
        slice_el = page.locator(".geetest_window:visible .geetest_slice_bg, .geetest_slide:visible .geetest_slice_bg, .geetest_slice_bg:visible").first
        btn_el = page.locator(".geetest_slider:visible .geetest_btn, .geetest_btn:visible").first

        await bg_el.wait_for(state="visible", timeout=60000)
        await slice_el.wait_for(state="visible", timeout=60000)
        await btn_el.wait_for(state="visible", timeout=60000)

        # 1. Extraction des images
        bg_bytes = await extract_image_bytes(bg_el)
        slice_bytes = await extract_image_bytes(slice_el)

        if not bg_bytes or not slice_bytes:
            print("  [!] Échec d'extraction des images du Captcha.")
            return False

        # 2. Calcul OpenCV de la position X
        t0 = asyncio.get_event_loop().time()
        solver = SlideSolver(puzzle_piece=slice_bytes, background=bg_bytes)
        native_x = solver.find_puzzle_piece_position()
        elapsed = round(asyncio.get_event_loop().time() - t0, 3)

        # 3. Échelle d'affichage
        bg_box = await bg_el.bounding_box()
        btn_box = await btn_el.bounding_box()

        if not bg_box or not btn_box:
            return False

        # Image native = 300px de large
        scale = bg_box["width"] / 300.0
        distance_x = native_x * scale
        print(f"  [✓] [OpenCV] X={native_x}px calculé en {elapsed}s (déplacement: {round(distance_x, 1)}px)...")

        # 4. Glisser-déposer réaliste (courbe d'accélération humaine)
        start_x = btn_box["x"] + btn_box["width"] / 2
        start_y = btn_box["y"] + btn_box["height"] / 2

        await page.mouse.move(start_x, start_y)
        await asyncio.sleep(0.08)
        await page.mouse.down()

        steps = 26
        for i in range(1, steps + 1):
            progress = i / steps
            ease = 1.0 - pow(1.0 - progress, 3)
            cur_x = start_x + (distance_x * ease)
            cur_y = start_y + random.uniform(-1.0, 1.0)
            await page.mouse.move(cur_x, cur_y)
            await asyncio.sleep(random.uniform(0.012, 0.022))

        await asyncio.sleep(random.uniform(0.12, 0.2))
        await page.mouse.up()
        print("  [✓] [OpenCV] Curseur relâché.")
        return True

    except Exception as e:
        print(f"  [!] Note auto-solveur OpenCV : {e}")
        return False

# ──────────────────────────────────────────────
#  Flux d'inscription Bybit
# ──────────────────────────────────────────────
async def run_bybit_flow(page, phone_number: str) -> bool:
    print(f"\n  [*] Numéro cible : {phone_number}")

    # Réinitialisation des journaux pour ce numéro
    browser_console_logs.clear()
    network_errors.clear()

    try:
        # Step 1 : Chargement direct
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1.0)

        # Acceptation systématique du bandeau cookies (essentiel pour la validation Akamai)
        try:
            cookie_btn = page.locator(
                "button:has-text('Accept All'), "
                "button:has-text('Accept'), "
                "button:has-text('Got it'), "
                "#onetrust-accept-btn-handler"
            ).first
            if await cookie_btn.is_visible(timeout=1500):
                await cookie_btn.click(timeout=2000)
                await asyncio.sleep(0.5)
        except Exception:
            pass

        # Détection d'un blocage géographique affiché directement par Bybit
        restricted_sel = (
            ":has-text('Service Restricted'), "
            ":has-text('not available in your jurisdiction'), "
            ":has-text('not available in your region'), "
            ":has-text('restricted in your location')"
        )
        if await page.locator(restricted_sel).first.is_visible():
            print("\n  [⛔ BLOCAGE GÉOGRAPHIQUE BYBIT] 'Service Restricted' détecté sur la page Bybit.")
            print_browser_diagnostics()
            return "RESTRICTED"

        # Step 2 : Champ de saisie ou clic sur 'Create Account'
        phone_input = page.locator("input[name='username']:visible, input[placeholder*='Email/Mobile']:visible").first

        if not await phone_input.is_visible():
            initial_btn = page.locator(
                "button.cht-register-login-button:visible, "
                "button:has-text('Create Account'):visible, "
                "button[data-test-btn='true']:visible, "
                "a:has-text('Create Account'):visible"
            ).first
            try:
                await initial_btn.wait_for(state="visible", timeout=6000)
                await initial_btn.click(force=True)
            except Exception:
                pass

        # Step 4 : Saisie du numéro avec clic préalable
        await phone_input.wait_for(state="visible", timeout=12000)
        await phone_input.click()
        await asyncio.sleep(0.3)
        await phone_input.fill(phone_number)
        await asyncio.sleep(0.4)

        # Step 4.5 : Vérification de l'indicatif Algérie (+213)
        prefix_sel = page.locator("[data-test-select_country='true']").first
        try:
            await prefix_sel.wait_for(state="visible", timeout=4000)
            current_prefix = (await prefix_sel.inner_text()).strip()

            if "+213" not in current_prefix:
                print(f"  [*] Indicatif '{current_prefix}' -> sélection Algérie (+213)...")
                await prefix_sel.click()

                search_input = page.locator(
                    "input[data-test-input_country='true'], input.index_citycode-search__v8D_N"
                ).first
                await search_input.wait_for(state="visible", timeout=3000)
                await search_input.fill("213")

                algeria_item = page.locator(
                    "li[data-test-country_item='true']:has-text('+213'), li:has-text('+213')"
                ).first
                await algeria_item.wait_for(state="visible", timeout=3000)
                await algeria_item.click()
                await asyncio.sleep(0.4)
            else:
                pass  # Déjà sur +213
        except Exception as e:
            print(f"  [!] Note sélection préfixe : {e}")

        # Step 5 : Soumission du formulaire avec délai et déplacement naturel (Akamai telemetry)
        submit_btn = page.locator(
            "button.cht-register-login-button:visible, "
            "button:has-text('Create Account'):visible, "
            "button[data-test-btn='true']:visible"
        ).first
        await submit_btn.wait_for(state="visible", timeout=10000)

        # Déplacement naturel de souris vers le bouton pour alimenter les capteurs Akamai
        btn_box = await submit_btn.bounding_box()
        if btn_box:
            await page.mouse.move(
                btn_box["x"] + btn_box["width"] / 2 + random.uniform(-5, 5),
                btn_box["y"] + btn_box["height"] / 2 + random.uniform(-3, 3),
                steps=8
            )

        # Pause d'interaction humaine (1.2s - 1.8s) avant le clic
        await asyncio.sleep(random.uniform(1.2, 1.8))
        await submit_btn.click()

        # Step 7 : Attente réactive de la modal de validation (Sans NopeCHA)
        geetest_sel = (
            ".geetest_box:visible, .geetest_window:visible, .geetest_slider:visible, "
            ".geetest_popup:visible, .geetest_panel:visible, iframe[src*='geetest']:visible"
        )
        modal_sel = (
            "[class*='otp-channel-container'], "
            "[class*='cht-temporary-custom-modal'], "
            ":has-text('Select verification method')"
        )

        modal_found = False
        captcha_reported = False
        start_time = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start_time) < CAPTCHA_TIMEOUT:
            # Détection de l'erreur Geetest 60500 (Captcha Forbidden)
            if await page.locator(":has-text('Captcha Forbidden'), :has-text('Error code: 60500')").first.is_visible():
                print("  [!] Alerte Geetest 60500 ('Captcha Forbidden') : IP restreinte temporairement ou détection bot.")
                print_browser_diagnostics()
                return False

            if await page.locator(modal_sel).first.is_visible():
                elapsed = round(asyncio.get_event_loop().time() - start_time, 1)
                print(f"  [✓] Étape pré-OTP validée en {elapsed}s.")
                modal_found = True
                break

            if not captcha_reported and await page.locator(geetest_sel).first.is_visible():
                captcha_reported = True
                if SKIP_ON_CAPTCHA:
                    print("  [!] Captcha Geetest détecté -> SKIP immédiat.")
                    return False
                print("  [*] Captcha Geetest détecté -> Résolution automatique par vision OpenCV...")
                await asyncio.sleep(0.5)
                solved = await auto_solve_geetest_slider(page)
                if solved:
                    print("  [*] Attente de la validation du puzzle...")
                    # Attente active de l'apparition de la modale SMS (jusqu'à 6s)
                    for _ in range(30):
                        if await page.locator(modal_sel).first.is_visible():
                            elapsed = round(asyncio.get_event_loop().time() - start_time, 1)
                            print(f"  [✓✓] Captcha résolu et validé avec succès en {elapsed}s via OpenCV !")
                            modal_found = True
                            break
                        await asyncio.sleep(0.2)

                    if modal_found:
                        break

                    # Si après 6s le bouton de glissement est encore présent, deuxième essai
                    if await page.locator(".geetest_window:visible .geetest_btn, .geetest_btn:visible").first.is_visible():
                        print("  [*] Deuxième essai d'alignement OpenCV...")
                        await auto_solve_geetest_slider(page)
                        for _ in range(20):
                            if await page.locator(modal_sel).first.is_visible():
                                modal_found = True
                                break
                            await asyncio.sleep(0.2)
                        if modal_found:
                            break
                else:
                    print(f"  [!] Puzzle non résolu automatiquement -> attente manuelle (max {CAPTCHA_TIMEOUT}s)...")
                    try:
                        await page.bring_to_front()
                    except Exception:
                        pass

            await asyncio.sleep(0.2)

        if not modal_found:
            modal = page.locator(modal_sel).first
            await modal.wait_for(state="visible", timeout=4000)

        # Step 8 : Sélection de l'option SMS (exclut strictement WhatsApp)
        print("  [*] Step 8 : Sélection du canal SMS...")
        # Pause naturelle pour laisser l'animation de la boîte de dialogue se stabiliser
        await asyncio.sleep(random.uniform(0.8, 1.2))

        # Cible strictement l'item SMS (en filtrant tout élément contenant Whatsapp ou la mention 'no SMS charges')
        sms_button = page.locator(
            "div[class*='otp-channel-item']:has(img[alt='sms']), "
            "div[class*='otp-channel-item']:has(img[src*='sms.png']), "
            "div[class*='otp-channel-item']:has-text('Traditional text message')"
        ).filter(has_not_text="Whatsapp").first

        await sms_button.wait_for(state="visible", timeout=8000)
        await sms_button.scroll_into_view_if_needed()

        # Mouvement de souris naturel vers l'option SMS
        box = await sms_button.bounding_box()
        if box:
            await page.mouse.move(
                box["x"] + box["width"] / 2 + random.uniform(-10, 10),
                box["y"] + box["height"] / 2 + random.uniform(-5, 5),
                steps=6
            )
            await asyncio.sleep(0.3)

        try:
            await sms_button.click(delay=100)
        except Exception:
            try:
                await sms_button.click(force=True)
            except Exception:
                await page.evaluate("""() => {
                    const smsImg = document.querySelector("img[alt='sms'], img[src*='sms.png']");
                    if (smsImg) {
                        const item = smsImg.closest("[class*='otp-channel-item']") || smsImg;
                        item.click();
                    }
                }""")
        print("  [✓] Clic sur l'option SMS effectué.")

        # Step 9 : Clic éventuel sur le bouton Confirm / Continue
        try:
            confirm_btn = page.locator(
                "button:has-text('Confirm'):visible, "
                "button:has-text('Continue'):visible, "
                "button:has-text('Next'):visible"
            ).first
            if await confirm_btn.is_visible(timeout=1000):
                await confirm_btn.click(force=True)
                print("  [✓] Confirmation effectuée.")
        except Exception:
            pass

        # Step 10 : Attente impérative de l'affichage COMPLET de la page de saisie d'OTP
        # (exclut strictement la page referral / code d'invitation)
        print("  [*] Step 10 : Attente de l'arrivée sur la page de saisie d'OTP...")

        # Sélecteurs dédiés à la saisie du code OTP (numérique / digits)
        otp_field_sel = (
            "input[autocomplete='one-time-code']:visible, "
            "input[data-test-input_code='true']:visible, "
            "div[class*='digit-input'] input:visible, "
            "div[class*='otp-input'] input:visible, "
            "div[class*='security-code'] input:visible, "
            "div[class*='verification'] input:visible, "
            "input[inputmode='numeric']:visible"
        )

        # Indicateurs textuels de l'écran OTP officiel Bybit
        otp_text_sel = (
            ":has-text('Enter verification code'):visible, "
            ":has-text('Verification Code'):visible, "
            ":has-text('Security Verification'):visible, "
            ":has-text('Enter the 6-digit'):visible, "
            ":has-text('6-digit code'):visible, "
            ":has-text('code sent to'):visible, "
            "button:has-text('Resend'):visible, "
            "span:has-text('Resend'):visible"
        )

        error_toast_sel = (
            "[class*='toast-error']:visible, "
            "[class*='ant-message-error']:visible, "
            ":has-text('Too many requests'):visible, "
            ":has-text('Operation too frequent'):visible, "
            ":has-text('Security risk'):visible, "
            ":has-text('couldn\\'t verify your request'):visible, "
            ":has-text('Refresh the page and try again'):visible"
        )

        otp_displayed = False
        start_otp_wait = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start_otp_wait) < OTP_PAGE_TIMEOUT:
            # 1. Vérifier si un refus / erreur Bybit est intervenu
            if await page.locator(error_toast_sel).first.is_visible():
                err_text = (await page.locator(error_toast_sel).first.inner_text()).strip()
                print(f"  [!] Erreur détectée lors de l'envoi OTP : {err_text}")
                print_browser_diagnostics()
                return False

            # 2. Vérifier la présence des cases de code OTP ou des textes spécifiques OTP
            has_field = await page.locator(otp_field_sel).first.is_visible()
            has_text = await page.locator(otp_text_sel).first.is_visible()

            if has_field or has_text:
                otp_displayed = True
                break

            await asyncio.sleep(0.3)

        if not otp_displayed:
            print(f"  [FAIL] La page de saisie d'OTP ne s'est pas affichée pour {phone_number}.")
            print_browser_diagnostics()
            return False

        # Attente de chargement complet du champ OTP pour confirmer le rendu
        try:
            await page.locator(otp_field_sel).first.wait_for(state="visible", timeout=4000)
        except Exception:
            pass

        # Stabilisation finale pour s'assurer que l'UI est 100% chargée
        await asyncio.sleep(1.2)

        print(f"  [✓✓✓] Page de saisie d'OTP COMPLÈTEMENT affichée pour {phone_number} !")
        return True

    except Exception as e:
        print(f"  [FAIL] Échec pour {phone_number} : {e}")
        print_browser_diagnostics()
        return False

# ──────────────────────────────────────────────
#  Préparation et gestion des sessions
# ──────────────────────────────────────────────
async def clear_cache_and_cookies(context, page):
    """
    Prépare le navigateur pour le numéro suivant :
    - Ferme les onglets popups ouverts.
    - Préserve les cookies de capteurs Akamai (_abck, bm_sz) et le consentement
      pour maintenir la réputation de confiance auprès du WAF de Bybit.
    """
    try:
        for extra_page in context.pages[1:]:
            try:
                await extra_page.close()
            except Exception:
                pass
    except Exception as e:
        print(f"  [!] Avertissement nettoyage : {e}")

# ──────────────────────────────────────────────
#  Boucle principale
# ──────────────────────────────────────────────
async def main():
    # ── 1. Vérification pré-vol de l'adresse IP et de la région ──
    print(f"\n{'═' * 55}")
    print("  [🌐] Vérification de l'adresse IP et de la région...")
    is_eligible, public_ip, country_name, country_code = verify_ip_country()

    if not is_eligible:
        country_label = BYBIT_RESTRICTED_COUNTRIES.get(country_code, country_name)
        print("\n" + "!" * 55)
        print("  [⛔ BLOCAGE DE SÉCURITÉ] IP PROVENANT D'UN PAYS RESTREINT")
        print("!" * 55)
        print(f"  • Adresse IP détectée : {public_ip}")
        print(f"  • Pays d'origine      : {country_name} [{country_code}] ({country_label})")
        print("  • Statut Bybit        : CE PAYS EST STRICTEMENT INTERDIT / NON SUPPORTÉ")
        print("\n  [Conséquence] :")
        print("  Bybit bloque toute inscription et envoi de code depuis cette juridiction.")
        print("\n  [Action requise] :")
        print("  Veuillez vous connecter via un réseau/proxy situé dans un pays éligible")
        print("  (ex: Algérie, Madagascar, etc.) avant de relancer le script.")
        print("!" * 55)
        print("  >> Arrêt immédiat du script pour préserver votre liste de numéros.\n")
        return

    print(f"  [✓] IP publique   : {public_ip}")
    print(f"  [✓] Géolocalisation: {country_name} [{country_code}] (Région éligible Bybit)")
    print(f"{'═' * 55}\n")

    all_numbers = load_algeria_numbers()
    total_csv = len(all_numbers)
    if total_csv == 0:
        print("[!] Aucun numéro valide trouvé dans le fichier CSV.")
        return

    # Tirage aléatoire parmi les numéros restants non encore traités
    remaining_numbers, total_already_processed = get_random_unprocessed_queue(all_numbers)
    total_remaining = len(remaining_numbers)

    print(f"\n{'═' * 55}")
    print(f"  Fichier CSV source         : {os.path.basename(CSV_FILE)}")
    print(f"  Total dans le CSV          : {total_csv} numéros")
    print(f"  Déjà traités (succès)      : {total_already_processed} numéros")
    print(f"  Restants à traiter         : {total_remaining} numéros")
    print(f"  Mode de sélection          : Aléatoire (shuffle)")
    print(f"{'═' * 55}\n")

    if total_remaining == 0:
        print("[✓] Tous les numéros du fichier CSV ont déjà été traités avec succès !")
        return

    os.makedirs(USER_DATA_DIR, exist_ok=True)
    reset_chrome_crash_state(USER_DATA_DIR)

    success = 0
    fail = 0

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            executable_path=CHROME_EXE if os.path.exists(CHROME_EXE) else None,
            channel="chrome" if not os.path.exists(CHROME_EXE) else None,
            headless=HEADLESS,
            ignore_default_args=["--no-sandbox", "--enable-automation"],
            args=[
                "--start-maximized",
                "--test-type",
                "--disable-blink-features=AutomationControlled",
                "--disable-session-crashed-bubble",
                "--hide-crash-restore-bubble",
            ]
        )

        # Masque proprement navigator.webdriver sans déclencher d'alerte de flag dans Chrome
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """)

        page = context.pages[0] if context.pages else await context.new_page()

        # Active la capture des logs et requêtes rejetées
        setup_browser_diagnostics(page)

        if BLOCK_USELESS_RESOURCES:
            await setup_route_blocking(page)

        for i, number in enumerate(remaining_numbers, 1):
            total_done = total_already_processed + success
            print(f"\n[Session : {i}/{total_remaining}] (Progression globale : {total_done}/{total_csv})")

            await clear_cache_and_cookies(context, page)
            res = await run_bybit_flow(page, number)

            if res == "RESTRICTED":
                print("\n  [⛔] Arrêt définitif : Bybit a refusé l'accès à cause de la région de l'IP.")
                break
            elif res is True:
                success += 1
                mark_number_success(number)
                print(f"  [💾] Numéro {number} validé et enregistré dans {os.path.basename(PROCESSED_FILE)}.")
            else:
                fail += 1

            if DELAY_BETWEEN_NUMBERS > 0:
                await asyncio.sleep(DELAY_BETWEEN_NUMBERS)

        await context.close()

    print(f"\n{'═' * 55}")
    print(f"  Session terminée : {success} succès | {fail} échecs | {total_remaining} traités")
    print(f"{'═' * 55}")

if __name__ == "__main__":
    asyncio.run(main())
