"""
Agente de marcação de aulas VivaGym -- versão API direta (sem browser).

Descoberto a partir dos ficheiros JS públicos da Área de Cliente
(middleware.vivagym.com), sem nunca autenticar como o Claude:
  - Login:        POST /api/v1/persons/get-data   {email, password} -> token
  - CSRF:         GET  /api/v1/csrf/<intencao>      -> token CSRF (por pedido)
  - Listar aulas: POST /api/v1/activities/filter    {gyms:[id], dateInterval}
  - Reservar:     POST /api/v1/activities/book      {gymId, activityId}
  - Ginásios:     GET  /api/v1/gyms (público, sem auth) -- usado para resolver
                  o nome do ginásio (ex: "Benfica") para o seu id numérico.

Modos de uso:
  python booker_api.py --discover     Faz login, resolve o ginásio, lista as
                                       aulas do dia alvo e mostra o resultado
                                       (não reserva nada).
  python booker_api.py --dry-run      Faz tudo incluindo encontrar a aula com
                                       vaga, mas não chama o endpoint de reserva.
  python booker_api.py                Corre a sequência real.
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

BASE_URL = "https://middleware.vivagym.com/public/pt-pt/api/v1"
RETRY_WINDOW_SECONDS = 90
RETRY_INTERVAL_SECONDS = 0.3
ARRIVE_EARLY_SECONDS = 20
# Se "agora" estiver a menos disto da abertura prevista, vale a pena esperar e
# fazer a rajada de tentativas nesta mesma execução (cobre o cron horário que
# calhe cair pouco antes da hora exata).
BURST_LOOKAHEAD_SECONDS = 45 * 60
LISBON_TZ = ZoneInfo("Europe/Lisbon")

CSRF_GET_DATA = "api_v1_person_get_data_public"
CSRF_FILTER = "api_v1_activities_filter_public"
CSRF_BOOK = "api_v1_activities_book_public"

DIAS_SEMANA = {
    "segunda": 0, "terca": 1, "terça": 1, "quarta": 2, "quinta": 3,
    "sexta": 4, "sabado": 5, "sábado": 5, "domingo": 6,
}


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join("logs", f"booker_api_{ts}.log")
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
    target_weekday = DIAS_SEMANA[day_name.strip().lower()]
    hh, mm = (int(x) for x in time_str.split(":"))
    now = now_lisbon()
    days_ahead = (target_weekday - now.weekday()) % 7
    candidate = datetime.combine(now.date() + timedelta(days=days_ahead), dtime(hh, mm), tzinfo=LISBON_TZ)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def resolve_gym_id(gym_name: str) -> int:
    """A lista de ginásios é pública -- não exige autenticação."""
    r = requests.get(f"{BASE_URL}/gyms", timeout=15)
    r.raise_for_status()
    tree = r.json()

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "Center" and node.get("name", "").strip().lower() == gym_name.strip().lower():
                return node.get("id")
            for child in node.get("children", []) or []:
                found = walk(child)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found is not None:
                    return found
        return None

    gym_id = walk(tree)
    if gym_id is None:
        raise RuntimeError(f"Não encontrei o ginásio '{gym_name}' na lista pública de ginásios.")
    return gym_id


def get_csrf(session: requests.Session, intention: str) -> str:
    r = session.get(f"{BASE_URL}/csrf/{intention}", timeout=15)
    r.raise_for_status()
    data = r.json()
    token = data.get("token")
    if not token:
        raise RuntimeError(f"Não recebi CSRF token para '{intention}': {data}")
    return token


def login(session: requests.Session, email: str, password: str) -> str:
    csrf = get_csrf(session, CSRF_GET_DATA)
    r = session.post(
        f"{BASE_URL}/persons/get-data",
        json={"email": email, "password": password},
        headers={"X-CSRF-Token": csrf},
        timeout=20,
    )
    data = r.json() if r.content else {}
    token = data.get("token")
    if not token:
        raise RuntimeError(f"Login falhou (status {r.status_code}): {data}")
    return token


def filter_activities(session: requests.Session, token: str, gym_id: int, date_str: str) -> list:
    csrf = get_csrf(session, CSRF_FILTER)
    r = session.post(
        f"{BASE_URL}/activities/filter",
        json={"gyms": [gym_id], "dateInterval": {"from": date_str, "to": date_str}},
        headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("activities", [])
    raise RuntimeError(f"Resposta inesperada de /activities/filter: {data!r}")


def book_activity(session: requests.Session, token: str, gym_id: int, activity_id: int):
    csrf = get_csrf(session, CSRF_BOOK)
    r = session.post(
        f"{BASE_URL}/activities/book",
        json={"gymId": gym_id, "activityId": activity_id},
        headers={"Authorization": f"Bearer {token}", "X-CSRF-Token": csrf},
        timeout=20,
    )
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"raw": r.text}


def find_matching_activity(activities: list, class_name: str, time_str: str):
    for a in activities:
        if a.get("name", "").strip().lower() == class_name.strip().lower() and a.get("startTime") == time_str:
            return a
    return None


def discover(session: requests.Session, token: str, gym_id: int, cfg: dict):
    target_dt = next_class_datetime(cfg["CLASS_DAY"], cfg["CLASS_TIME"])
    date_str = target_dt.date().isoformat()
    logging.info(f"A listar aulas em '{cfg['GYM_NAME']}' (id={gym_id}) para {date_str}...")
    activities = filter_activities(session, token, gym_id, date_str)
    logging.info(f"{len(activities)} aulas encontradas nesse dia.")

    os.makedirs("logs", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join("logs", f"discover_api_{ts}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(activities, f, ensure_ascii=False, indent=2)
    logging.info(f"Guardei a resposta completa em: {out_path}")

    for a in activities:
        capacity = a.get("classCapacity", 0)
        booked = a.get("bookedCount", 0)
        logging.info(
            f"  {a.get('startTime')}-{a.get('endTime')} {a.get('name')} "
            f"({capacity - booked}/{capacity} vagas) bookingId={a.get('bookingId')}"
        )

    match = find_matching_activity(activities, cfg["CLASS_NAME"], cfg["CLASS_TIME"])
    if match:
        logging.info(f"Aula alvo encontrada: {json.dumps(match, ensure_ascii=False)}")
    else:
        logging.warning("Não encontrei a aula alvo nesse dia (pode ainda não estar publicada, ou nome/hora não batem certo).")


def attempt_booking(session: requests.Session, token: str, gym_id: int, cfg: dict, date_str: str, dry_run: bool) -> bool:
    deadline = time.monotonic() + RETRY_WINDOW_SECONDS
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            activities = filter_activities(session, token, gym_id, date_str)
        except Exception as e:
            logging.warning(f"[tentativa {attempt}] Erro ao consultar o horário: {e}")
            time.sleep(RETRY_INTERVAL_SECONDS)
            continue

        match = find_matching_activity(activities, cfg["CLASS_NAME"], cfg["CLASS_TIME"])
        if match is None:
            logging.info(f"[tentativa {attempt}] Aula ainda não aparece no horário. A repetir...")
            time.sleep(RETRY_INTERVAL_SECONDS)
            continue

        capacity = match.get("classCapacity", 0)
        booked = match.get("bookedCount", 0)
        available = capacity - booked
        booking_id = match.get("bookingId") or {}

        if available <= 0 or not booking_id.get("id"):
            logging.info(f"[tentativa {attempt}] Sem vagas ainda ({available}/{capacity}). A repetir...")
            time.sleep(RETRY_INTERVAL_SECONDS)
            continue

        logging.info(f"[tentativa {attempt}] Vaga disponível ({available}/{capacity}).")
        if dry_run:
            logging.info("DRY RUN: não vou chamar o endpoint de reserva.")
            return True

        status, result = book_activity(session, token, booking_id["center"], booking_id["id"])
        if status < 300 and not result.get("error"):
            logging.info(f"Reserva confirmada (status {status}): {result}")
            return True
        logging.warning(f"[tentativa {attempt}] Falha ao reservar (status {status}): {result}")
        time.sleep(RETRY_INTERVAL_SECONDS)

    logging.error("Esgotado o tempo de tentativas sem sucesso.")
    return False


def attempt_single_check(session: requests.Session, token: str, gym_id: int, cfg: dict, date_str: str) -> bool:
    """Uma única verificação leve (sem rajada) -- usada nas passagens horárias fora
    da janela de abertura prevista, para apanhar aberturas atrasadas ou cancelamentos."""
    activities = filter_activities(session, token, gym_id, date_str)
    match = find_matching_activity(activities, cfg["CLASS_NAME"], cfg["CLASS_TIME"])
    if match is None:
        logging.info("Verificação horária: aula ainda não está publicada no horário.")
        return False

    capacity = match.get("classCapacity", 0)
    booked = match.get("bookedCount", 0)
    available = capacity - booked
    booking_id = match.get("bookingId") or {}

    if available <= 0 or not booking_id.get("id"):
        logging.info(f"Verificação horária: ainda sem vagas ({available}/{capacity}).")
        return False

    logging.info(f"Verificação horária: vaga disponível ({available}/{capacity})! A reservar...")
    status, result = book_activity(session, token, booking_id["center"], booking_id["id"])
    if status < 300 and not result.get("error"):
        logging.info(f"Reserva confirmada (status {status}): {result}")
        return True
    logging.warning(f"Falha ao reservar (status {status}): {result}")
    return False


def is_already_booked(session: requests.Session, token: str, class_name: str, class_date: str) -> bool:
    """Verifica se já existe uma reserva BOOKED para essa data (evita tentar de novo
    e reenviar notificações depois de já termos conseguido). Falha em modo seguro:
    qualquer erro/formato inesperado é tratado como 'não sei', para nunca bloquear
    uma tentativa real por engano."""
    try:
        r = session.get(f"{BASE_URL}/activities", headers={"Authorization": f"Bearer {token}"}, timeout=15)
        if r.status_code >= 300:
            return False
        data = r.json()
        items = data.get("activities") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return False
        for item in items:
            if item.get("state") != "BOOKED":
                continue
            booking = item.get("booking") or {}
            if str(booking.get("date", "")).startswith(class_date):
                return True
        return False
    except Exception as e:
        logging.info(f"Não consegui confirmar reservas existentes (a assumir que não está reservada): {e}")
        return False


def notify_windows(title: str, message: str, duration_ms: int = 15000):
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


def wait_until(target_dt: datetime):
    logging.info(f"A aguardar até {target_dt.isoformat()} ...")
    while True:
        remaining = (target_dt - now_lisbon()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 5 if remaining > 5 else 0.02))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config()

    class_dt = next_class_datetime(cfg["CLASS_DAY"], cfg["CLASS_TIME"])
    open_dt = class_dt - timedelta(days=7)
    logging.info(f"Próxima aula alvo: {class_dt.isoformat()} | Abertura de reserva (PRIME, 7 dias): {open_dt.isoformat()}")

    is_real_run = not args.discover and not args.dry_run
    class_label = f"{cfg['CLASS_NAME']} ({cfg['CLASS_DAY']} {cfg['CLASS_TIME']}, {cfg['GYM_NAME']})"

    try:
        gym_id = resolve_gym_id(cfg["GYM_NAME"])
        logging.info(f"Ginásio '{cfg['GYM_NAME']}' resolvido para id={gym_id}.")

        session = requests.Session()
        token = login(session, cfg["VIVAGYM_EMAIL"], cfg["VIVAGYM_PASSWORD"])
        logging.info("Login OK.")

        if args.discover:
            discover(session, token, gym_id, cfg)
            return

        date_str = class_dt.date().isoformat()

        if is_real_run and is_already_booked(session, token, cfg["CLASS_NAME"], date_str):
            logging.info("Já está reservada para esta data -- nada a fazer nesta execução.")
            return

        arrive_at = open_dt - timedelta(seconds=ARRIVE_EARLY_SECONDS)
        seconds_to_arrive = (arrive_at - now_lisbon()).total_seconds()

        if 0 <= seconds_to_arrive <= BURST_LOOKAHEAD_SECONDS:
            # Estamos perto o suficiente da abertura prevista -- vale a pena esperar
            # e disparar a rajada de tentativas nesta mesma execução.
            wait_until(arrive_at)
            wait_until(open_dt)
            success = attempt_booking(session, token, gym_id, cfg, date_str, dry_run=args.dry_run)
            logging.info("RESULTADO: SUCESSO" if success else "RESULTADO: FALHOU")
            if is_real_run:
                if success:
                    notify("VivaGym - Reserva confirmada", f"Reserva efetuada com sucesso: {class_label}.")
                else:
                    notify(
                        "VivaGym - Reserva falhou",
                        f"Não consegui reservar {class_label} na hora exata da abertura. "
                        f"Vou continuar a verificar de hora a hora (pode ser que a abertura tenha atrasado, "
                        f"ou que apareça uma vaga por cancelamento). Logs em logs/.",
                    )
        elif seconds_to_arrive < 0:
            # Já passou a hora prevista de abertura (ou esta execução é uma
            # passagem horária de rede de segurança) -- só uma verificação leve,
            # sem rajada nem spam de notificações se ainda não houver vagas.
            success = attempt_single_check(session, token, gym_id, cfg, date_str)
            logging.info("RESULTADO: SUCESSO" if success else "RESULTADO: sem novidade")
            if is_real_run and success:
                notify("VivaGym - Reserva confirmada", f"Reserva efetuada com sucesso: {class_label}.")
        else:
            logging.info(
                f"Ainda faltam {seconds_to_arrive:.0f}s para a janela de abertura prevista "
                f"-- fora do alcance desta execução, não faço nada."
            )
    except Exception as e:
        logging.exception("Erro inesperado durante a execução.")
        if is_real_run:
            notify("VivaGym - Erro no agente", f"Erro ao tentar reservar {class_label}: {e}")
        raise


if __name__ == "__main__":
    main()
