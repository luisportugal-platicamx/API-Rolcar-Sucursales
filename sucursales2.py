"""
Rolcar Sucursales API
Usa Playwright para extraer sucursales directamente de https://www.rolcar.com/direcciones_old/
haciendo clic en el tab del estado solicitado.
"""

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import math
import json
import os
import sys
import time
import re
import asyncio
import threading
import requests
from playwright.sync_api import sync_playwright

app = FastAPI(
    title="Rolcar Sucursales API",
    description="Extrae sucursales Rolcar desde su sitio web usando Playwright, con ordenamiento por proximidad.",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ROLCAR_URL = "https://www.rolcar.com/direcciones_old/"

# Mapa de aliases → nombre exacto del tab en la página
STATE_TAB_MAP = {
    # nombre exacto del tab (tal como aparece en data-title / texto del botón)
    "aguascalientes":   "Aguascalientes",
    "campeche":         "Campeche",
    "chihuahua":        "Chihuahua",
    "coahuila":         "Coahuila",
    "colima":           "Colima",
    "durango":          "Durango",
    "guanajuato":       "Guanajuato",
    "jalisco":          "Jalisco",
    "mexico":           "Mexico",
    "estado de mexico": "Mexico",
    "edomex":           "Mexico",
    "cdmx":             "Mexico",
    "michoacan":        "Michoacan",
    "michoacán":        "Michoacan",
    "morelos":          "Morelos",
    "nayarit":          "Nayarit",
    "nuevo leon":       "Nuevo Leon",
    "nuevo león":       "Nuevo Leon",
    "nl":               "Nuevo Leon",
    "puebla":           "Puebla",
    "quintana roo":     "Quintana Roo",
    "qroo":             "Quintana Roo",
    "queretaro":        "Queretaro",
    "querétaro":        "Queretaro",
    "qro":              "Queretaro",
    "san luis":         "San Luis",
    "san luis potosi":  "San Luis",
    "san luis potosí":  "San Luis",
    "slp":              "San Luis",
    "tabasco":          "Tabasco",
    "tamaulipas":       "Tamaulipas",
    "tamps":            "Tamaulipas",
    "tlaxcala":         "Tlaxcala",
    "veracruz":         "Veracruz",
    "ver":              "Veracruz",
    "yucatan":          "Yucatán",
    "yucatán":          "Yucatán",
    "yuc":              "Yucatán",
    # Aliases cortos
    "ags":   "Aguascalientes",
    "camp":  "Campeche",
    "chih":  "Chihuahua",
    "coah":  "Coahuila",
    "col":   "Colima",
    "dgo":   "Durango",
    "gto":   "Guanajuato",
    "jal":   "Jalisco",
    "mex":   "Mexico",
    "mich":  "Michoacan",
    "mor":   "Morelos",
    "nay":   "Nayarit",
    "pue":   "Puebla",
    "tlax":  "Tlaxcala",
}

# Cache de geocodificación en disco
GEOCACHE_FILE = os.path.join(os.path.dirname(__file__), "geocache.json")
geocache: dict = {}

def load_geocache():
    global geocache
    if os.path.exists(GEOCACHE_FILE):
        try:
            with open(GEOCACHE_FILE, "r", encoding="utf-8") as f:
                geocache = json.load(f)
        except Exception:
            geocache = {}

def save_geocache():
    with open(GEOCACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(geocache, f, ensure_ascii=False, indent=2)

load_geocache()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def normalize_state(state: str) -> str | None:
    """Devuelve el nombre exacto del tab (ej. 'Nuevo Leon') o None."""
    s = state.strip().lower()
    return STATE_TAB_MAP.get(s)


def parse_branch_text(p_text: str) -> dict:
    """
    Dado el texto de un <p> de sucursal, extrae dirección y teléfono.
    El texto viene separado por \\n (de los <br> del HTML).
    Ejemplo:
        Av. Convención Nte. No. 1810
        Fracc. Circunvalación Nte. C.P. 20020
        Tel. 449 914-1155, Fax 449 914-1680
        aguascalientes@rolcar.com.mx
    """
    lines = [l.strip() for l in p_text.splitlines() if l.strip()]
    
    telefono = ""
    email_idx = None
    tel_idx = None

    for i, line in enumerate(lines):
        # Detectar línea de teléfono
        if re.search(r"tel[éeÉE]?\.?\s*[\d]", line, re.IGNORECASE) or \
           re.search(r"^\d{3}\s*\d{3}", line):
            tel_idx = i
            # Extraer solo números y separadores del campo de teléfono
            # Quitar "Tel.", "Fax", etc. para quedarnos con el primer número
            telefono_raw = re.sub(r"(?i)tel\.?\s*|fax\.?\s*[\d\s\-]*|fax\s*", "", line)
            telefono = telefono_raw.strip().rstrip(",").strip()
            if not telefono:
                # Intentar extraer el primer número de teléfono de la línea original
                nums = re.findall(r"[\d]{3}[\s\-][\d]{3}[\-\d\s]+", line)
                telefono = nums[0].strip() if nums else line
        # Detectar email
        if "@" in line:
            email_idx = i

    # Construir dirección: todo lo que NO sea teléfono ni email
    dir_lines = []
    for i, line in enumerate(lines):
        if i == tel_idx or i == email_idx:
            continue
        # Saltar líneas que son solo teléfono adicional (sin "Tel" pero con formato numérico)
        if tel_idx is not None and i > tel_idx and re.match(r"^[\d\s\-,]+$", line):
            continue
        dir_lines.append(line)

    direccion = ", ".join(dir_lines)
    return {"direccion": direccion, "telefono": telefono}


def _scrape_in_thread(tab_name: str) -> list[dict]:
    """
    Versión síncrona del scraping que corre en un thread dedicado.
    Así evitamos el conflicto de event loops entre uvicorn y Playwright en Windows.
    """
    branches = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Bloquear imágenes y fuentes para cargar más rápido
        page.route("**/*.{png,jpg,jpeg,gif,svg,woff,woff2,ttf}",
                   lambda route: route.abort())

        page.goto(ROLCAR_URL, wait_until="domcontentloaded", timeout=30000)

        # Hacer clic en el tab del estado
        tab_selector = f".su-tabs-nav span:has-text('{tab_name}')"
        page.click(tab_selector, timeout=10000)

        # Esperar que el pane esté activo
        pane_selector = f".su-tabs-pane[data-title='{tab_name}']"
        page.wait_for_selector(f"{pane_selector}.su-tabs-pane-open", timeout=10000)

        # Extraer tarjetas
        cards = page.query_selector_all(f"{pane_selector} h4.ubicaciones")

        for card in cards:
            name_el = card.query_selector("strong")
            if not name_el:
                continue
            nombre = name_el.inner_text().strip()

            parent = card.evaluate_handle(
                "el => el.closest('.su-column-inner') || el.parentElement"
            )
            p_el = parent.query_selector("p")
            if not p_el:
                continue

            p_text = p_el.inner_text()
            parsed = parse_branch_text(p_text)

            branches.append({
                "nombre": nombre,
                "direccion": parsed["direccion"],
                "telefono": parsed["telefono"],
            })

        browser.close()

    return branches


def scrape_state_branches(tab_name: str) -> list[dict]:
    """
    Lanza el scraping en un thread separado para no interferir
    con el event loop de uvicorn/FastAPI (problema en Windows).
    """
    result = []
    error = []

    def run():
        try:
            result.extend(_scrape_in_thread(tab_name))
        except Exception as e:
            error.append(e)

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=60)

    if error:
        raise error[0]
    return result


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def geocode_address(address: str) -> tuple[float, float] | None:
    """Geocodifica una dirección con Nominatim (con caché en disco)."""
    if address in geocache:
        cached = geocache[address]
        return tuple(cached) if cached else None

    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": address, "format": "json", "limit": 1, "countrycodes": "mx"}
        headers = {"User-Agent": "RolcarSucursalesAPI/2.0 (agente-ia)"}
        resp = requests.get(url, params=params, headers=headers, timeout=6)
        data = resp.json()
        if data:
            lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
            geocache[address] = [lat, lon]
            save_geocache()
            time.sleep(1.1)  # Respetar rate limit de Nominatim
            return lat, lon
        geocache[address] = None
        save_geocache()
        return None
    except Exception:
        return None


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "mensaje": "Rolcar Sucursales API v2 (Playwright)",
        "uso": "GET /sucursales?estado=aguascalientes",
        "docs": "/docs",
    }


@app.get("/estados")
def list_states():
    """Lista todos los estados disponibles (nombres de tab exactos)."""
    estados = sorted(set(STATE_TAB_MAP.values()))
    return {"estados": estados}


@app.get("/sucursales")
async def get_branches(
    estado: str = Query(
        ...,
        description="Nombre del estado. Ej: 'Aguascalientes', 'Nuevo Leon', 'jal', 'nl'"
    ),
    ubicacion: Optional[str] = Query(
        None,
        description="Dirección del usuario en texto. Ej: 'Calle Morelos 45, Col. Centro, Aguascalientes'"
    ),
    limite: Optional[int] = Query(
        None,
        ge=1, le=100,
        description="Número máximo de sucursales a devolver"
    ),
):
    tab_name = normalize_state(estado)
    if not tab_name:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Estado '{estado}' no reconocido. "
                f"Usa /estados para ver los disponibles, o prueba aliases como: "
                f"'ags', 'nl', 'jal', 'gto', 'qro', etc."
            )
        )

    # Scraping en thread separado (compatible con Windows + uvicorn)
    try:
        loop = asyncio.get_event_loop()
        sucursales = await loop.run_in_executor(None, scrape_state_branches, tab_name)
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Error al obtener sucursales de la página de Rolcar: {str(e)}"
        )

    if not sucursales:
        raise HTTPException(
            status_code=404,
            detail=f"No se encontraron sucursales para el estado '{tab_name}'."
        )

    total = len(sucursales)
    user_coords = None
    ubicacion_resuelta = None

    # Geocodificar la dirección del usuario si se proporcionó
    if ubicacion:
        user_coords = geocode_address(ubicacion)
        if user_coords is None:
            user_coords = geocode_address(f"{ubicacion}, México")
        if user_coords:
            ubicacion_resuelta = ubicacion

    # Ordenar por distancia si se resolvió la ubicación
    if user_coords:
        user_lat, user_lng = user_coords
        con_dist = []
        sin_dist = []
        for s in sucursales:
            coords = geocode_address(s["direccion"])
            if coords:
                distancia = haversine_km(user_lat, user_lng, coords[0], coords[1])
                con_dist.append({**s, "distancia_km": round(distancia, 2)})
            else:
                sin_dist.append({**s, "distancia_km": None})

        con_dist.sort(key=lambda x: x["distancia_km"])
        sucursales = con_dist + sin_dist

    # Aplicar límite
    if limite:
        sucursales = sucursales[:limite]

    return {
        "estado": tab_name,
        "total_sucursales_en_estado": total,
        "sucursales_devueltas": len(sucursales),
        "ordenadas_por_proximidad": user_coords is not None,
        "ubicacion_usuario": ubicacion_resuelta,
        "advertencia": (
            f"No se pudo geocodificar la dirección '{ubicacion}'. "
            "Las sucursales se muestran sin ordenar por proximidad."
        ) if ubicacion and not user_coords else None,
        "sucursales": sucursales,
    }