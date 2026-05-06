"""
Interfaz para sincronizar citas desde un CSV a GoHighLevel.
Autónoma: no requiere editar ningún fichero Python.
"""

import csv
import json
import io
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

CONFIG_PATH = Path(__file__).parent / "config.json"
TIMEZONE    = "Europe/Madrid"
BASE_URL    = "https://services.leadconnectorhq.com"

# ─── Config ───────────────────────────────────────────────────────────────────

DEFAULT_SHIFTS   = [{"apertura": "09:30", "cierre": "13:30"}]
DEFAULT_DURATION = 20

def _env_staff() -> list[dict]:
    raw = os.getenv("GHL_STAFF_IDS", "")
    if not raw:
        return []
    return [{"nombre": k, "user_id": v} for k, v in json.loads(raw).items()]

def load_config() -> dict:
    # Prioridad: config.json > .env > defaults
    base = {
        "api_token":    os.getenv("GHL_API_TOKEN",    ""),
        "location_id":  os.getenv("GHL_LOCATION_ID",  ""),
        "calendar_id":  os.getenv("GHL_CALENDAR_ID",  ""),
        "staff":        _env_staff(),
        "shifts":       DEFAULT_SHIFTS,
        "slot_duration": DEFAULT_DURATION,
    }
    if CONFIG_PATH.exists():
        stored = json.loads(CONFIG_PATH.read_text())
        if "morning_start" in stored and "shifts" not in stored:
            stored["shifts"] = [
                {"apertura": stored["morning_start"],   "cierre": stored["morning_end"]},
                {"apertura": stored["afternoon_start"], "cierre": stored["afternoon_end"]},
            ]
        base.update(stored)
    return base

def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))

# ─── Slot logic ───────────────────────────────────────────────────────────────

def build_slots(shifts: list[dict], duration: int) -> list[time]:
    slots = []
    for shift in shifts:
        cur   = datetime.strptime(shift["apertura"], "%H:%M")
        limit = datetime.strptime(shift["cierre"],   "%H:%M") - timedelta(minutes=duration)
        while cur <= limit:
            slots.append(cur.time())
            cur += timedelta(minutes=duration)
    return slots

def normalize_slot(raw: time, taken: set, valid_slots: list[time]) -> time | None:
    def mins(t): return t.hour * 60 + t.minute
    raw_m = mins(raw)
    for slot in sorted(valid_slots, key=lambda s: (abs(mins(s) - raw_m), mins(s))):
        if slot not in taken:
            return slot
    return None

def preprocess_csv(rows: list[dict], valid_slots: list[time], duration: int) -> list[dict]:
    taken: dict[tuple, set] = {}
    result = []
    for row in rows:
        date_str    = row["Date"]
        profesional = row["Profesional"].strip()
        raw_start   = datetime.strptime(row["Tarea"].strip(), "%H:%M").time()
        key = (date_str, profesional)
        taken.setdefault(key, set())
        slot = normalize_slot(raw_start, taken[key], valid_slots)
        if slot is None:
            continue
        taken[key].add(slot)
        slot_str = slot.strftime("%H:%M")
        end_str  = (datetime.combine(datetime.today(), slot) + timedelta(minutes=duration)).strftime("%H:%M")
        result.append({
            "Fecha":       date_str,
            "Paciente":    row["Paciente"].strip(),
            "Telefono":    row["Teléfono"].strip(),
            "Profesional": profesional,
            "Original":    row["Tarea"].strip(),
            "Start":       slot_str,
            "End":         end_str,
            "ajustado":    slot_str != row["Tarea"].strip(),
        })
    return result

# ─── API helpers ──────────────────────────────────────────────────────────────

def make_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Version": "2021-04-15"}

def upsert_contact(name, phone, location_id, headers) -> str | None:
    parts = name.split()
    base  = {"locationId": location_id, "firstName": parts[0].title() if parts else "Sin nombre",
             "lastName": " ".join(parts[1:]).title() if len(parts) > 1 else "", "name": name.title()}
    if phone and phone != "-":
        r = requests.post(f"{BASE_URL}/contacts/upsert", headers=headers, json={**base, "phone": phone})
    else:
        r = requests.post(f"{BASE_URL}/contacts/", headers=headers, json=base)
    if r.status_code in (200, 201):
        data = r.json()
        return data.get("contact", data).get("id")
    return None

def get_day_appointments(date_str, location_id, calendar_id, headers) -> list[dict]:
    tz = ZoneInfo(TIMEZONE)
    d  = datetime.strptime(date_str, "%d/%m/%Y")
    r  = requests.get(f"{BASE_URL}/calendars/events", headers=headers, params={
        "locationId": location_id, "calendarId": calendar_id,
        "startTime":  int(d.replace(hour=0,  minute=0,  tzinfo=tz).timestamp() * 1000),
        "endTime":    int(d.replace(hour=23, minute=59, tzinfo=tz).timestamp() * 1000),
    })
    return r.json().get("events", []) if r.status_code == 200 else []

def find_existing(appointments, contact_id, start) -> str | None:
    ms = int(start.timestamp() * 1000)
    for a in appointments:
        if a.get("contactId") == contact_id and a.get("startTime") == ms:
            return a.get("id")
    return None

def create_appointment(contact_id, start, end, staff_id, location_id, calendar_id, headers) -> str | None:
    r = requests.post(f"{BASE_URL}/calendars/events/appointments", headers=headers, json={
        "calendarId": calendar_id, "locationId": location_id, "contactId": contact_id,
        "startTime": start.isoformat(), "endTime": end.isoformat(),
        "assignedUserId": staff_id, "ignoreDateRange": False, "toNotify": False,
    })
    if r.status_code in (200, 201):
        d = r.json()
        return d.get("id") or d.get("appointment", {}).get("id")
    return None

def update_appointment(event_id, start, end, staff_id, location_id, calendar_id, headers) -> bool:
    r = requests.put(f"{BASE_URL}/calendars/events/appointments/{event_id}", headers=headers, json={
        "calendarId": calendar_id, "locationId": location_id,
        "startTime": start.isoformat(), "endTime": end.isoformat(), "assignedUserId": staff_id,
    })
    return r.status_code in (200, 201)

# ─── App ──────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Citas GHL", layout="wide", page_icon="🦷")

st.markdown("""
<style>
    [data-testid="stSidebar"] { min-width: 380px; max-width: 500px; }
</style>
""", unsafe_allow_html=True)

st.title("Citas → GoHighLevel")

cfg = load_config()

# ── 1. CSV uploader ───────────────────────────────────────────────────────────

uploaded = st.file_uploader("Sube el CSV de citas", type="csv")

raw_rows: list[dict] = []
csv_professionals: list[str] = []

if uploaded is not None:
    content = uploaded.read()
    raw_rows = list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
    csv_professionals = sorted({r["Profesional"].strip() for r in raw_rows})

# ── 2. Sidebar ────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("Configuración GHL")

    api_token   = st.text_input("API Token",   value=cfg["api_token"],   type="password")
    location_id = st.text_input("Location ID", value=cfg["location_id"])
    calendar_id = st.text_input("Calendar ID", value=cfg["calendar_id"])

    st.divider()
    st.markdown("**Profesionales**")
    st.caption("Los nombres se cargan del CSV. Añade el User ID de cada uno.")

    saved_ids  = {s["nombre"]: s["user_id"] for s in cfg["staff"]}
    staff_data = (
        [{"nombre": p, "user_id": saved_ids.get(p, "")} for p in csv_professionals]
        if csv_professionals else
        (cfg["staff"] or [{"nombre": "", "user_id": ""}])
    )

    edited_staff = st.data_editor(
        pd.DataFrame(staff_data),
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "nombre":  st.column_config.TextColumn("Nombre en CSV", disabled=bool(csv_professionals)),
            "user_id": st.column_config.TextColumn("User ID en GHL"),
        },
        hide_index=True,
        key="staff_editor",
    )

    st.divider()
    st.markdown("**Horario de la clínica**")
    st.caption("Añade una fila por franja horaria (mañana, tarde, noche…)")

    shifts_df = pd.DataFrame(cfg["shifts"])
    edited_shifts = st.data_editor(
        shifts_df,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "apertura": st.column_config.TextColumn("Apertura", help="HH:MM"),
            "cierre":   st.column_config.TextColumn("Cierre",   help="HH:MM"),
        },
        hide_index=True,
        key="shifts_editor",
    )

    slot_duration = st.number_input(
        "Duración de cita (min)", min_value=5, max_value=120, step=5,
        value=int(cfg["slot_duration"]),
    )

    try:
        valid_shifts  = edited_shifts.dropna().to_dict("records")
        preview_slots = build_slots(valid_shifts, slot_duration)
        st.caption(f"{len(preview_slots)} slots disponibles por profesional/día")
    except Exception:
        preview_slots = []
        valid_shifts  = []
        st.error("Formato de hora incorrecto. Usa HH:MM.")

    st.divider()

    if st.button("Guardar configuración", use_container_width=True, type="primary"):
        save_config({
            "api_token":    api_token,
            "location_id":  location_id,
            "calendar_id":  calendar_id,
            "staff":        edited_staff.dropna(subset=["nombre"]).to_dict("records"),
            "shifts":       valid_shifts,
            "slot_duration": slot_duration,
        })
        cfg = load_config()
        st.success("Guardado")

    staff_map = {r["nombre"]: r["user_id"] for _, r in edited_staff.iterrows() if r["nombre"] and r["user_id"]}
    config_ok = bool(api_token and location_id and calendar_id and staff_map and preview_slots)

    if not config_ok:
        st.warning("Rellena todos los campos para sincronizar.")

# ── 3. Procesar CSV con el horario activo ─────────────────────────────────────

if not raw_rows:
    st.info("Sube un fichero CSV para continuar.")
    st.stop()

@st.cache_data
def get_records(rows_json: str, shifts_json: str, dur: int) -> list[dict]:
    slots = build_slots(json.loads(shifts_json), dur)
    return preprocess_csv(json.loads(rows_json), slots, dur)

try:
    records = get_records(
        json.dumps(raw_rows,    ensure_ascii=False),
        json.dumps(valid_shifts, ensure_ascii=False),
        slot_duration,
    )
except Exception as e:
    st.error(f"Error procesando el CSV: {e}")
    st.stop()

# ── 4. Métricas + tabla ───────────────────────────────────────────────────────

if "statuses" not in st.session_state or len(st.session_state.statuses) != len(records):
    st.session_state.statuses = {i: "Pendiente" for i in range(len(records))}

c1, c2, c3, c4 = st.columns(4)
c1.metric("Total citas",   len(records))
c2.metric("Ajustadas",     sum(r["ajustado"] for r in records))
c3.metric("Sin teléfono",  sum(r["Telefono"] == "-" for r in records))
c4.metric("Sincronizadas", sum(1 for s in st.session_state.statuses.values() if s.startswith("OK")))

st.divider()

df = pd.DataFrame([{
    "Fecha": r["Fecha"], "Paciente": r["Paciente"], "Teléfono": r["Telefono"],
    "Profesional": r["Profesional"], "Start": r["Start"], "End": r["End"],
    "Ajustado": "Si" if r["ajustado"] else "",
    "Estado": st.session_state.statuses[i],
} for i, r in enumerate(records)])

st.dataframe(df, use_container_width=True, height=460, hide_index=True,
    column_config={
        "Ajustado": st.column_config.TextColumn(width=80),
        "Estado":   st.column_config.TextColumn(width=140),
        "Start":    st.column_config.TextColumn(width=70),
        "End":      st.column_config.TextColumn(width=70),
    })

st.divider()

col_run, col_reset = st.columns([4, 1])
run   = col_run.button("Sincronizar con GHL", type="primary", use_container_width=True, disabled=not config_ok)
reset = col_reset.button("Resetear estados", use_container_width=True)

if reset:
    st.session_state.statuses = {i: "Pendiente" for i in range(len(records))}
    st.rerun()

# ── 5. Sync ───────────────────────────────────────────────────────────────────

if run:
    headers = make_headers(api_token)
    tz  = ZoneInfo(TIMEZONE)
    bar = st.progress(0, text="Cargando citas existentes de GHL...")

    cached: dict[str, list] = {}
    for date_str in {r["Fecha"] for r in records}:
        cached[date_str] = get_day_appointments(date_str, location_id, calendar_id, headers)

    stats = {"created": 0, "updated": 0, "errors": 0}
    log   = st.container()

    for i, row in enumerate(records):
        bar.progress((i + 1) / len(records), text=f"{i+1}/{len(records)}: {row['Paciente']}")
        slot     = datetime.strptime(row["Start"], "%H:%M").time()
        staff_id = staff_map.get(row["Profesional"])

        if not staff_id:
            st.session_state.statuses[i] = "Staff desconocido"
            log.warning(f"{row['Paciente']}: '{row['Profesional']}' sin User ID en el sidebar.")
            continue

        start_dt   = datetime.strptime(row["Fecha"], "%d/%m/%Y").replace(hour=slot.hour, minute=slot.minute, tzinfo=tz)
        end_dt     = start_dt + timedelta(minutes=slot_duration)
        contact_id = upsert_contact(row["Paciente"], row["Telefono"], location_id, headers)

        if not contact_id:
            st.session_state.statuses[i] = "Error contacto"
            stats["errors"] += 1
            log.error(f"{row['Paciente']}: no se pudo crear/actualizar el contacto.")
            continue

        existing_id = find_existing(cached.get(row["Fecha"], []), contact_id, start_dt)

        if existing_id:
            ok = update_appointment(existing_id, start_dt, end_dt, staff_id, location_id, calendar_id, headers)
            if ok:
                st.session_state.statuses[i] = "OK Actualizada"
                stats["updated"] += 1
            else:
                st.session_state.statuses[i] = "Error cita"
                stats["errors"] += 1
                log.error(f"{row['Paciente']}: error al actualizar.")
        else:
            new_id = create_appointment(contact_id, start_dt, end_dt, staff_id, location_id, calendar_id, headers)
            if new_id:
                st.session_state.statuses[i] = "OK Creada"
                stats["created"] += 1
            else:
                st.session_state.statuses[i] = "Error cita"
                stats["errors"] += 1
                log.error(f"{row['Paciente']}: error al crear la cita.")

    bar.empty()

    if stats["errors"] == 0:
        st.success(f"Completado — {stats['created']} creadas, {stats['updated']} actualizadas.")
    else:
        st.warning(f"Completado con errores — Creadas: {stats['created']}, Actualizadas: {stats['updated']}, Errores: {stats['errors']}")

    st.rerun()
