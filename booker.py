"""
Agente de marcação de aulas VivaGym.

Modos de uso:
  python booker.py --discover        Faz login e mapeia a Área de Cliente (para afinar seletores).
  python booker.py --dry-run         Faz tudo menos o clique final de reserva.
  python booker.py                   Corre a sequência real: login, espera pela abertura, tenta reservar.
  Acrescenta --headed a qualquer modo para veres o browser (útil para testar).
"""

import argparse
import base64
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

LOGIN_URL = "https://www.vivagym.com/pt-pt/members/login/"
BOOKINGS_URL = "https://www.vivagym.com/pt-pt/members/bookings/"
RETRY_WINDOW_SECONDS = 90
RETRY_INTERVAL_SECONDS = 0.5
ARRIVE_EARLY_SECONDS = 60
LISBON_TZ = ZoneInfo("Europe/Lisbon")

DIAS_SEMANA = {
    "segunda": 0, "terca": 1, "terça": 1, "quarta": 2, "quinta": 3,
    "sexta": 4, "sabado": 5, "sábado": 5, "domingo": 6,
}


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join("logs", f"booker_{ts}.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )
    return log_path


def load_config():
    load_dotenv()
    required = ["VIVAGYM_EMAIL", "VIVAGYM_PASSWORD", "GYM_NAME", "CLASS_NAME", "CLASS_DAY", "CLASS_TIME"]
    cfg = {k: os.getenv(k) for k in required}
    missing = [k for k, v in cfg.items() if not v]
    if missing:
        raise SystemExit(
            f"Faltam variáveis no .env: {', '.join(missing)}. "
            f"Copia .env.example para .env e preenche os valores."
        )
    return cfg


def now_lisbon() -> datetime:
    return datetime.now(LISBON_TZ)


def next_class_datetime(day_name: str, time_str: str) -> datetime:
    """Devolve o próximo horário da aula, sempre em hora de Lisboa (independente do fuso horário da máquina que corre o script)."""
    target_weekday = DIAS_SEMANA[day_name.strip().lower()]
    hh, mm = (int(x) for x in time_str.split(":"))
    now = now_lisbon()
    days_ahead = (target_weekday - now.weekday()) % 7
    candidate = datetime.combine(now.date() + timedelta(days=days_ahead), dtime(hh, mm), tzinfo=LISBON_TZ)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def dismiss_cookie_banner(page, timeout=4000):
    try:
        btn = page.get_by_role("button", name="Aceitar todos os cookies")
        btn.wait_for(state="visible", timeout=timeout)
        btn.click()
        logging.info("Banner de cookies aceite.")
    except Exception:
        pass


def login(page, email: str, password: str):
    logging.info("A abrir página de login...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    dismiss_cookie_banner(page)
    page.get_by_placeholder("E-mail").fill(email)
    page.get_by_placeholder("Palavra-passe").fill(password)
    logging.info("A submeter credenciais...")
    page.get_by_role("button", name="Iniciar sessão").click()

    try:
        page.wait_for_url(lambda url: "/members/login" not in url, timeout=15000)
    except PWTimeout:
        pass

    if "/members/login" in page.url:
        error_text = page.inner_text("body")[:500]
        raise RuntimeError(f"Login parece ter falhado, continuamos na página de login. Conteúdo: {error_text}")

    logging.info(f"Login OK. URL atual: {page.url}")


def dump_page(page, out_dir, label):
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    screenshot_path = os.path.join(out_dir, f"discover_{label}_{ts}.png")
    page.screenshot(path=screenshot_path, full_page=True)

    links = page.eval_on_selector_all(
        "a", "els => els.map(e => ({text: e.innerText.trim(), href: e.href})).filter(x => x.text)"
    )
    buttons = page.eval_on_selector_all(
        "button", "els => els.map(e => e.innerText.trim()).filter(Boolean)"
    )
    # texto de todos os elementos "folha" (sem filhos) -- ajuda a ver nomes de aulas/horas/dias
    # mesmo que não sejam links nem botões (ex: divs de uma grelha de horário)
    leaf_texts = page.eval_on_selector_all(
        "body *",
        "els => els.filter(e => e.children.length === 0).map(e => e.innerText && e.innerText.trim()).filter(Boolean)",
    )

    data = {
        "url": page.url,
        "title": page.title(),
        "links": links,
        "buttons": buttons,
        "leaf_texts": leaf_texts[:500],
    }
    json_path = os.path.join(out_dir, f"discover_{label}_{ts}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    logging.info(f"[{label}] Screenshot: {screenshot_path}")
    logging.info(f"[{label}] Mapa da página: {json_path}")
    return screenshot_path, json_path


def dump_schedule_html(page, out_dir, label):
    """Captura o outerHTML à volta de uma linha de aula (ex: onde diz 'vagas disponíveis'),
    para se ver a marcação exata do botão de reserva (normalmente um ícone sem texto)."""
    try:
        html = page.eval_on_selector(
            "body",
            """body => {
                const el = Array.from(body.querySelectorAll('*')).find(
                    e => e.children.length === 0 && /vagas dispon/i.test(e.textContent || '')
                );
                if (!el) return null;
                let node = el;
                for (let i = 0; i < 5 && node.parentElement; i++) node = node.parentElement;
                return node.outerHTML;
            }""",
        )
    except Exception as e:
        html = None
        logging.warning(f"Não consegui capturar o HTML do calendário: {e}")

    if html:
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(out_dir, f"discover_{label}_schedule_{ts}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        logging.info(f"[{label}] HTML do calendário de aulas: {path}")
    else:
        logging.warning(f"[{label}] Não encontrei nenhuma linha com 'vagas disponíveis' nesta página/dia.")


def set_day_filter(page, target_date) -> bool:
    """Muda o filtro 'Dia' do calendário para a data indicada e clica em Filtrar.

    O input[type=date] real (data-testid=bookings-filter-date-inline) está escondido
    atrás de um datepicker customizado, por isso o Playwright recusa .fill() normal
    (exige visibilidade). Escrevemos o valor via JS, usando o setter nativo do
    HTMLInputElement, para que o React/framework da app apanhe o evento 'input' na mesma.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    try:
        date_input = page.locator('[data-testid="bookings-filter-date-inline"]').first
        if not date_input.count():
            date_input = page.locator('input[type="date"]').first
        if not date_input.count():
            logging.warning("Não encontrei o campo de data.")
            return False

        date_input.evaluate(
            """(el, value) => {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            date_str,
        )
        page.wait_for_timeout(500)

        filtrar = page.get_by_role("button", name="Filtrar")
        if filtrar.count():
            filtrar.click()
        page.wait_for_load_state("networkidle", timeout=15000)

        try:
            page.wait_for_selector("text=/vagas dispon/i", timeout=8000)
        except PWTimeout:
            logging.warning(
                "Não apareceu nenhuma linha com 'vagas disponíveis' depois de filtrar "
                "(pode ser normal se não houver aulas nesse dia/ginásio, ou o filtro pode não ter aplicado)."
            )

        logging.info(f"Filtro de dia definido para {date_str}.")
        return True
    except Exception as e:
        logging.warning(f"Não consegui definir o filtro de dia: {e}")
        return False


def discover(page, out_dir="logs", cfg=None):
    page.wait_for_load_state("networkidle", timeout=15000)
    dump_page(page, out_dir, "dashboard")

    logging.info(f"A navegar para a página de reservas: {BOOKINGS_URL}")
    page.goto(BOOKINGS_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle", timeout=15000)
    dump_page(page, out_dir, "bookings_hoje")
    dump_schedule_html(page, out_dir, "bookings_hoje")

    if cfg:
        target_dt = next_class_datetime(cfg["CLASS_DAY"], cfg["CLASS_TIME"])
        logging.info(f"A mudar o filtro de dia para {target_dt.date()} ({cfg['CLASS_DAY']})...")
        if set_day_filter(page, target_dt.date()):
            dump_page(page, out_dir, "bookings_dia_alvo")
            dump_schedule_html(page, out_dir, "bookings_dia_alvo")

    logging.info("Envia estes ficheiros (não contêm a password) para afinarmos os seletores de reserva.")


def goto_booking_area(page):
    logging.info(f"A navegar para a página de reservas: {BOOKINGS_URL}")
    page.goto(BOOKINGS_URL, wait_until="domcontentloaded")
    dismiss_cookie_banner(page, timeout=1500)
    page.wait_for_load_state("networkidle", timeout=15000)
    return True


def select_gym(page, gym_name: str) -> bool:
    """A conta do utilizador já mostra 'Benfica' como ginásio por omissão no filtro de reservas,
    por isso isto é só um ajuste best-effort -- não bloqueia o fluxo se o ginásio já estiver certo ou se falhar."""
    try:
        select = page.locator("select").filter(has=page.locator(f'option:text-is("{gym_name}")')).first
        if not select.count():
            return False
        if select.input_value() != gym_name:
            select.select_option(label=gym_name)
            page.wait_for_load_state("networkidle", timeout=10000)
            logging.info(f"Ginásio alterado para '{gym_name}'.")
        return True
    except Exception as e:
        logging.info(f"Não confirmei/alterei o ginásio automaticamente (assumindo que já está correto): {e}")
    return False


def find_class_row(page, class_name: str, time_str: str):
    """Procura a linha da aula (bookings-calendar__item) pelo nome + hora de início.
    Devolve o locator do botão de reserva (bookings-calendar__book-btn), ou None se a linha não existir."""
    try:
        rows = page.locator("div.bookings-calendar__item")
        count = rows.count()
        for i in range(count):
            row = rows.nth(i)
            name = row.locator("h3.bookings-calendar__name").inner_text(timeout=2000).strip()
            start = row.locator(".bookings-calendar__time-start").inner_text(timeout=2000).strip()
            if name.lower() == class_name.strip().lower() and start == time_str.strip():
                btn = row.locator("button.bookings-calendar__book-btn").first
                if btn.count():
                    return btn
    except Exception as e:
        logging.warning(f"Erro ao procurar a linha da aula: {e}")
    return None


def is_button_bookable(btn) -> tuple[bool, str]:
    try:
        aria_label = btn.get_attribute("aria-label") or ""
        is_disabled = btn.get_attribute("disabled") is not None
        bookable = (not is_disabled) and aria_label.lower().startswith("reservar")
        return bookable, aria_label
    except Exception as e:
        return False, f"erro ao ler estado do botão: {e}"


def refresh_schedule(page):
    """Volta a clicar em 'Filtrar' para atualizar as vagas sem perder o filtro de dia/ginásio
    (ao contrário de um page.reload(), que reporia o filtro para 'hoje')."""
    try:
        filtrar = page.get_by_role("button", name="Filtrar")
        if filtrar.count():
            filtrar.click()
            page.wait_for_load_state("networkidle", timeout=8000)
    except Exception as e:
        logging.warning(f"Falha ao atualizar o calendário: {e}")


def attempt_booking(page, cfg, dry_run: bool) -> bool:
    deadline = time.monotonic() + RETRY_WINDOW_SECONDS
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        btn = find_class_row(page, cfg["CLASS_NAME"], cfg["CLASS_TIME"])
        bookable, state = (False, "linha da aula não encontrada")
        if btn is not None:
            bookable, state = is_button_bookable(btn)

        if bookable:
            logging.info(f"[tentativa {attempt}] Reservável agora (aria-label='{state}').")
            if dry_run:
                logging.info("DRY RUN: não vou clicar. Fim do teste.")
                return True
            try:
                btn.click(timeout=3000)
                page.wait_for_timeout(1000)
                logging.info("Clique efetuado. Verifica manualmente a confirmação na app/site.")
                return True
            except Exception as e:
                logging.warning(f"[tentativa {attempt}] Falha ao clicar: {e}")
        else:
            logging.info(f"[tentativa {attempt}] Ainda não reservável ({state}). A atualizar...")

        refresh_schedule(page)
        time.sleep(RETRY_INTERVAL_SECONDS)

    logging.error("Esgotado o tempo de tentativas sem sucesso.")
    return False


def notify_windows(title: str, message: str, duration_ms: int = 15000):
    """Mostra uma notificação nativa do Windows com o resultado (sucesso ou falha)."""
    title_b64 = base64.b64encode(title.encode("utf-8")).decode("ascii")
    message_b64 = base64.b64encode(message.encode("utf-8")).decode("ascii")
    ps_script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$title = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{title_b64}'))
$message = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{message_b64}'))
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Information
$notify.Visible = $true
$notify.ShowBalloonTip({duration_ms}, $title, $message, [System.Windows.Forms.ToolTipIcon]::Info)
Start-Sleep -Milliseconds {duration_ms + 500}
$notify.Dispose()
"""
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            timeout=30,
            capture_output=True,
        )
    except Exception as e:
        logging.warning(f"Não consegui mostrar a notificação do Windows: {e}")


def wait_until(target_dt: datetime):
    logging.info(f"A aguardar até {target_dt.isoformat()} ...")
    while True:
        remaining = (target_dt - now_lisbon()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 5 if remaining > 5 else 0.02))


def notify_email(subject: str, body: str):
    api_key = os.getenv("RESEND_API_KEY")
    to_addr = os.getenv("NOTIFY_EMAIL_TO")
    if not api_key or not to_addr:
        logging.info("RESEND_API_KEY/NOTIFY_EMAIL_TO não configurados — a saltar notificação por email.")
        return
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from": "VivaGym Bot <onboarding@resend.dev>",
                "to": [to_addr],
                "subject": subject,
                "text": body,
            },
            timeout=15,
        )
        if resp.status_code >= 300:
            logging.warning(f"Falha ao enviar email ({resp.status_code}): {resp.text}")
        else:
            logging.info("Email de notificação enviado.")
    except Exception as e:
        logging.warning(f"Erro ao enviar email de notificação: {e}")


def notify(title: str, message: str):
    notify_email(title, message)
    if os.name == "nt":
        notify_windows(title, message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config()

    class_dt = next_class_datetime(cfg["CLASS_DAY"], cfg["CLASS_TIME"])
    open_dt = class_dt - timedelta(days=7)
    logging.info(f"Próxima aula alvo: {class_dt.isoformat()} | Abertura de reserva (PRIME, 7 dias): {open_dt.isoformat()}")

    is_real_run = not args.discover and not args.dry_run
    class_label = f"{cfg['CLASS_NAME']} ({cfg['CLASS_DAY']} {cfg['CLASS_TIME']}, {cfg['GYM_NAME']})"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page()
        try:
            login(page, cfg["VIVAGYM_EMAIL"], cfg["VIVAGYM_PASSWORD"])

            if args.discover:
                discover(page, cfg=cfg)
                return

            arrive_at = open_dt - timedelta(seconds=ARRIVE_EARLY_SECONDS)
            if now_lisbon() < arrive_at:
                wait_until(arrive_at)

            goto_booking_area(page)
            select_gym(page, cfg["GYM_NAME"])
            set_day_filter(page, class_dt.date())

            if now_lisbon() < open_dt:
                wait_until(open_dt)

            success = attempt_booking(page, cfg, dry_run=args.dry_run)
            logging.info("RESULTADO: SUCESSO" if success else "RESULTADO: FALHOU")

            if is_real_run:
                if success:
                    notify("VivaGym - Reserva confirmada", f"Reserva efetuada com sucesso: {class_label}.")
                else:
                    notify("VivaGym - Reserva falhou", f"Não consegui reservar {class_label}. Verifica os logs em logs/.")
        except Exception as e:
            logging.exception("Erro inesperado durante a execução.")
            if is_real_run:
                notify("VivaGym - Erro no agente", f"Erro ao tentar reservar {class_label}: {e}")
            raise
        finally:
            if not args.headed:
                browser.close()
            else:
                logging.info("Browser em modo --headed fica aberto para inspecionares. Fecha manualmente quando quiseres.")


if __name__ == "__main__":
    main()
