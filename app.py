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

CONFIG_PATH  = Path(__file__).parent / "config.json"
TIMEZONE     = "Europe/Madrid"
BASE_URL     = "https://services.leadconnectorhq.com"
DIAS_SEMANA  = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

# ─── Config ───────────────────────────────────────────────────────────────────

DEFAULT_DURATION = 20

def _env_staff() -> list[dict]:
    raw = os.getenv("GHL_STAFF_IDS", "")
    if not raw:
        return []
    return [{"nombre": k, "user_id": v} for k, v in json.loads(raw).items()]

def load_config() -> dict:
    base = {
        "api_token":     os.getenv("GHL_API_TOKEN",   ""),
        "location_id":   os.getenv("GHL_LOCATION_ID", ""),
        "calendar_id":   os.getenv("GHL_CALENDAR_ID", ""),
        "staff":         _env_staff(),
        "slot_duration": DEFAULT_DURATION,
        "doctor_shifts": {},
    }
    if CONFIG_PATH.exists():
        stored = json.loads(CONFIG_PATH.read_text())
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

def get_doctor_slots(profesional: str, date_str: str, doctor_shifts: dict, duration: int) -> list[time]:
    weekday = datetime.strptime(date_str, "%d/%m/%Y").weekday()
    shifts  = [s for s in doctor_shifts.get(profesional, []) if int(s["dia"]) == weekday]
    return build_slots(shifts, duration)

def shifts_to_csv(shifts: dict) -> str:
    rows = [
        {"Profesional": prof, "Dia": DIAS_SEMANA[s["dia"]], "Apertura": s["apertura"], "Cierre": s["cierre"]}
        for prof, shift_list in shifts.items()
        for s in shift_list
    ]
    return pd.DataFrame(rows).to_csv(index=False)

def parse_shifts_csv(content: bytes) -> dict[str, list[dict]]:
    df = pd.read_csv(io.StringIO(content.decode("utf-8")))
    result: dict[str, list[dict]] = {}
    for _, row in df.iterrows():
        prof     = str(row["Profesional"]).strip()
        dia_name = str(row["Dia"]).strip()
        if dia_name not in DIAS_SEMANA:
            continue
        result.setdefault(prof, []).append({
            "dia":      DIAS_SEMANA.index(dia_name),
            "apertura": str(row["Apertura"]).strip(),
            "cierre":   str(row["Cierre"]).strip(),
        })
    return result

def normalize_phone(phone: str) -> str:
    if not phone or phone == "-":
        return phone
    digits = phone.replace(" ", "").replace("-", "")
    if digits.startswith("+"):
        return digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    if len(digits) == 9 and digits[0] in "6789":
        return "+34" + digits
    return phone

def preprocess_csv(rows: list[dict], doctor_shifts: dict, duration: int) -> list[dict]:
    taken: dict[tuple, set] = {}
    result = []
    for row in rows:
        date_str    = row["Date"]
        profesional = row["Profesional"].strip()
        raw_time_str = row["Tarea"].strip()
        try:
            raw_start = datetime.strptime(raw_time_str, "%H:%M").time()
        except ValueError:
            continue
        phone = normalize_phone(row["Teléfono"].strip())
        key   = (date_str, profesional)
        taken.setdefault(key, set())

        valid_slots = get_doctor_slots(profesional, date_str, doctor_shifts, duration)

        sin_horario = not valid_slots
        if valid_slots:
            slot = normalize_slot(raw_start, taken[key], valid_slots)
            if slot is None:
                continue
        else:
            slot = raw_start

        taken[key].add(slot)
        slot_str = slot.strftime("%H:%M")
        end_str  = (datetime.combine(datetime.today(), slot) + timedelta(minutes=duration)).strftime("%H:%M")
        result.append({
            "Fecha":       date_str,
            "Paciente":    row["Paciente"].strip(),
            "Telefono":    phone,
            "Profesional": profesional,
            "Original":    raw_time_str,
            "Start":       slot_str,
            "End":         end_str,
            "ajustado":    slot_str != raw_time_str,
            "sin_horario": sin_horario,
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
    # Normalizar fechas a DD/MM/YYYY por si vienen con dígitos simples (3/8/2026 → 03/08/2026)
    for r in raw_rows:
        try:
            parts = r["Date"].split("/")
            r["Date"] = f"{int(parts[0]):02d}/{int(parts[1]):02d}/{parts[2]}"
        except Exception:
            pass
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
        width="stretch",
        column_config={
            "nombre":  st.column_config.TextColumn("Nombre en CSV", disabled=bool(csv_professionals)),
            "user_id": st.column_config.TextColumn("User ID en GHL"),
        },
        hide_index=True,
        key="staff_editor",
    )

    slot_duration = st.number_input(
        "Duración de cita (min)", min_value=5, max_value=120, step=5,
        value=int(cfg["slot_duration"]),
    )

    st.divider()
    st.markdown("**Horarios por profesional**")

    shifts_file = st.file_uploader("Importar horarios (CSV)", type="csv", key="shifts_uploader",
                                   help="Columnas: Profesional, Dia, Apertura, Cierre")
    saved_shifts = cfg.get("doctor_shifts", {})

    if shifts_file is not None:
        try:
            saved_shifts = parse_shifts_csv(shifts_file.read())
            st.success("Horarios importados — pulsa Guardar configuración para persistirlos.")
        except Exception as e:
            st.error(f"Error al leer el CSV de horarios: {e}")

    st.caption("Puedes editar las franjas directamente en la tabla de cada médico.")
    all_doctors   = csv_professionals or [s["nombre"] for s in cfg["staff"] if s.get("nombre")]
    edited_doctor_shifts: dict[str, list[dict]] = {}

    for doctor in all_doctors:
        has_shifts = bool(saved_shifts.get(doctor))
        with st.expander(doctor, expanded=not has_shifts):
            saved_rows = saved_shifts.get(doctor, [])
            # Convertir número de día → nombre para mostrar en la UI
            display_rows = [
                {"dia": DIAS_SEMANA[r["dia"]], "apertura": r["apertura"], "cierre": r["cierre"]}
                for r in saved_rows
            ] if saved_rows else []
            empty_cols   = pd.DataFrame(columns=["dia", "apertura", "cierre"])
            default_rows = pd.DataFrame(display_rows) if display_rows else empty_cols
            edited_df = st.data_editor(
                default_rows,
                num_rows="dynamic",
                width="stretch",
                column_config={
                    "dia":      st.column_config.SelectboxColumn("Día", options=DIAS_SEMANA, required=True),
                    "apertura": st.column_config.TextColumn("Apertura (HH:MM)"),
                    "cierre":   st.column_config.TextColumn("Cierre (HH:MM)"),
                },
                hide_index=True,
                key=f"shifts_{doctor}",
            )
            try:
                rows_clean = [
                    # Convertir nombre de día → número para guardar/procesar
                    {"dia": DIAS_SEMANA.index(r["dia"]), "apertura": r["apertura"], "cierre": r["cierre"]}
                    for r in edited_df.dropna(subset=["dia", "apertura", "cierre"]).to_dict("records")
                    if r["dia"] in DIAS_SEMANA
                ]
                edited_doctor_shifts[doctor] = rows_clean
                total = sum(len(build_slots([r], slot_duration)) for r in rows_clean)
                st.caption(f"{total} slots semanales")
            except Exception:
                edited_doctor_shifts[doctor] = saved_shifts.get(doctor, [])
                st.error("Formato incorrecto en horario.")

    st.divider()

    if st.button("Guardar configuración", width="stretch", type="primary"):
        save_config({
            "api_token":     api_token,
            "location_id":   location_id,
            "calendar_id":   calendar_id,
            "staff":         edited_staff.dropna(subset=["nombre"]).to_dict("records"),
            "slot_duration": slot_duration,
            "doctor_shifts": edited_doctor_shifts,
        })
        cfg = load_config()
        st.success("Guardado")

    staff_map = {r["nombre"]: r["user_id"] for _, r in edited_staff.iterrows() if r["nombre"] and r["user_id"]}
    config_ok = bool(api_token and location_id and calendar_id and staff_map)

    if not config_ok:
        st.warning("Rellena todos los campos para sincronizar.")

# ── 3. Recalcular citas ───────────────────────────────────────────────────────

if not raw_rows:
    st.info("Sube un fichero CSV para continuar.")
    st.stop()

@st.cache_data
def get_records(rows_json: str, doctor_shifts_json: str, dur: int) -> list[dict]:
    return preprocess_csv(json.loads(rows_json), json.loads(doctor_shifts_json), dur)

if "records" not in st.session_state:
    st.session_state.records    = []
    st.session_state.calculated = False

col_calc, col_info = st.columns([2, 5])
if col_calc.button("Recalcular citas", type="primary", width="stretch"):
    try:
        st.session_state.records = get_records(
            json.dumps(raw_rows,             ensure_ascii=False),
            json.dumps(edited_doctor_shifts, ensure_ascii=False),
            slot_duration,
        )
        st.session_state.calculated = True
        st.session_state.statuses   = {i: "Pendiente" for i in range(len(st.session_state.records))}
    except Exception as e:
        st.error(f"Error procesando el CSV: {e}")
        st.stop()

if not st.session_state.calculated:
    col_info.info("Configura los horarios en la sidebar y pulsa **Recalcular citas** para ver y ajustar los slots.")
    st.stop()

records = st.session_state.records

# ── 4. Métricas + tabla ───────────────────────────────────────────────────────

if len(st.session_state.statuses) != len(records):
    st.session_state.statuses = {i: "Pendiente" for i in range(len(records))}

sin_horario_total = sum(r.get("sin_horario", False) for r in records)
if sin_horario_total:
    st.warning(
        f"{sin_horario_total} cita(s) sin horario configurado — se usa la hora del CSV tal cual. "
        "Configura los turnos en la sidebar y vuelve a recalcular."
    )

c1, c2, c3, c4 = st.columns(4)
c1.metric("Total citas",   len(records))
c2.metric("Ajustadas",     sum(r.get("ajustado", False) for r in records))
c3.metric("Sin teléfono",  sum(r["Telefono"] == "-" for r in records))
c4.metric("Sincronizadas", sum(1 for s in st.session_state.statuses.values() if s.startswith("OK")))

st.divider()

df = pd.DataFrame([{
    "Fecha": r["Fecha"], "Paciente": r["Paciente"], "Teléfono": r["Telefono"],
    "Profesional": r["Profesional"], "Start": r["Start"], "End": r["End"],
    "Slot": "ajustado" if r.get("ajustado") else ("sin horario" if r.get("sin_horario") else "ok"),
    "Estado": st.session_state.statuses[i],
} for i, r in enumerate(records)])

st.dataframe(df, width="stretch", height=460, hide_index=True,
    column_config={
        "Slot":   st.column_config.TextColumn(width=100),
        "Estado": st.column_config.TextColumn(width=140),
        "Start":    st.column_config.TextColumn(width=70),
        "End":      st.column_config.TextColumn(width=70),
    })

st.divider()

col_run, col_reset = st.columns([4, 1])
run   = col_run.button("Sincronizar con GHL", type="primary", width="stretch", disabled=not config_ok)
reset = col_reset.button("Resetear estados", width="stretch")

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
