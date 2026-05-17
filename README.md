# Citas → GoHighLevel

Herramienta para sincronizar citas desde un CSV exportado del sistema de gestión a GoHighLevel (GHL) sin tocar código.

**App desplegada:** [ghlappointmentsmanager.streamlit.app](https://ghlappointmentsmanager.streamlit.app/)

---

## Cómo usarla

### 1. Sube el CSV de citas

El CSV debe tener las siguientes columnas:

| Columna       | Descripción                        |
|---------------|------------------------------------|
| `Date`        | Fecha en formato `DD/MM/YYYY`      |
| `Paciente`    | Nombre completo del paciente       |
| `Teléfono`    | Número de teléfono (español o con prefijo) |
| `Profesional` | Nombre del profesional tal cual aparece en GHL |
| `Tarea`       | Hora de la cita en formato `HH:MM` |

### 2. Configura el sidebar

Al subir el CSV, los profesionales se detectan automáticamente. Solo tienes que:

1. Añadir el **User ID de GHL** de cada profesional (se guarda para futuras sesiones).
2. Configurar los **horarios semanales** de cada profesional (franjas de apertura y cierre por día).
3. Pulsar **Guardar configuración**.

### 3. Recalcula y sincroniza

- Pulsa **Recalcular citas** para previsualizar los slots ajustados.
- Revisa la tabla (columna `Slot` indica si alguna hora fue ajustada al slot válido más cercano).
- Pulsa **Sincronizar con GHL** para crear o actualizar las citas en GoHighLevel.

---

## Lógica de slots

- Cada cita se ajusta al slot válido más cercano dentro del horario del profesional.
- Si el slot exacto está ocupado, se asigna el siguiente libre.
- Los teléfonos españoles de 9 dígitos se normalizan automáticamente a formato `+34XXXXXXXXX`.

---

## Configuración local (opcional)

Si prefieres ejecutarlo en local:

```bash
pip install -r requirements.txt
```

Crea un fichero `.env` con:

```env
GHL_API_TOKEN=tu_token
GHL_LOCATION_ID=tu_location_id
GHL_CALENDAR_ID=tu_calendar_id
GHL_STAFF_IDS={"Nombre Profesional": "user_id_ghl"}
```

Lanza la app:

```bash
streamlit run app.py
```

---

## Archivos

| Archivo               | Descripción                                         |
|-----------------------|-----------------------------------------------------|
| `app.py`              | Interfaz Streamlit principal                        |
| `upsert_citas_ghl.py` | Script CLI alternativo (sin interfaz)               |
| `config.json`         | Configuración persistida (profesionales y horarios) |
| `requirements.txt`    | Dependencias Python                                 |
