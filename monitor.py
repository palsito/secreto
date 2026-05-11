#!/usr/bin/env python3
"""
Monitor de perfumedigital.es
- Monitoriza categorías fijas (paginadas por PASE)
- Detecta automáticamente nuevas páginas de oferta numeradas (oferta815, oferta816...)
- Notifica por Telegram
"""

import requests
from bs4 import BeautifulSoup
import json
import os
import re
import time
from datetime import datetime

# ─── CONFIGURACIÓN ────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE_URL = "https://perfumedigital.es"

# Categorías fijas a monitorizar
CATEGORIAS = [
    {
        "nombre": "🏷️ Outlet / Liquidación",
        "url": f"{BASE_URL}/index.php?ID_CATEGORIA=e5cb35910ddb33ceee5124e79cf89c93",
    },
    # Añade más aquí si quieres:
    # { "nombre": "🔥 Otra", "url": f"{BASE_URL}/index.php?ID_CATEGORIA=XXXX" },
]

# Páginas de oferta numeradas
OFERTA_BASE_URL   = f"{BASE_URL}/oferta"   # se le concatena el número + .html
OFERTA_NUMERO_KEY = "ultimo_numero_oferta" # clave en el estado
OFERTA_INICIO     = 815                    # número desde el que empezamos a vigilar

STATE_FILE = "estado_productos_perfumedigital.json"
HEADERS    = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
# ──────────────────────────────────────────────────────────────────

session = requests.Session()
session.headers.update(HEADERS)


# ── Estado ──────────────────────────────────────────────────────────

def cargar_estado():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def guardar_estado(estado):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


# ── Scraping ─────────────────────────────────────────────────────────

def parsear_productos_html(html):
    """Extrae productos de una página de perfumedigital."""
    soup = BeautifulSoup(html, "html.parser")
    productos = {}

    # Cada producto está en una celda <td align="center" style="width:33%;">
    celdas = soup.select('td[align="center"][style*="width:33%"]')

    for celda in celdas:
        # Enlace con el ID del producto
        link = celda.select_one('a[href*="op=descripcion"]')
        if not link:
            continue

        href = link.get("href", "")
        match = re.search(r"id=(\d+)", href)
        if not match:
            continue

        producto_id = match.group(1)

        # Nombre: buscamos el link dentro de .vam (más fiable que el de la imagen)
        vam = celda.select_one(".vam")
        if vam:
            nombre_link = vam.select_one('a[href*="op=descripcion"]')
            nombre = nombre_link.get_text(strip=True) if nombre_link else ""
        else:
            nombre = link.get_text(strip=True)

        if not nombre:
            continue

        # URL completa
        full_url = href if href.startswith("http") else f"{BASE_URL}/{href.lstrip('/')}"

        # Precio: dentro de .productSpecialPrice — tiene precio tachado + precio final
        precio_elem = celda.select_one(".productSpecialPrice")
        if precio_elem:
            texto = precio_elem.get_text(separator=" ", strip=True)
            precios = re.findall(r"\d+[.,]\d+", texto)
            # El último número es el precio final (el primero es el tachado)
            precio = f"{precios[-1].replace(',', '.')} €" if precios else texto
        else:
            precio_elem2 = celda.select_one(".productPrice, .price")
            precio = precio_elem2.get_text(strip=True) if precio_elem2 else "Sin precio"

        productos[producto_id] = {
            "nombre": nombre,
            "precio": precio,
            "url": full_url,
            "en_stock": True,  # no hay indicador en el listado
        }

    return productos


def scrape_categoria(url):
    """Descarga todos los productos de una categoría paginada con PASE=0,1,2..."""
    productos = {}
    pase = 0
    base = url.split("&PASE=")[0]

    while True:
        sep = "&" if "?" in base else "?"
        url_pag = f"{base}{sep}PASE={pase}"

        try:
            r = session.get(url_pag, timeout=15)
            r.raise_for_status()
        except Exception as e:
            print(f"  ⚠️  Error en {url_pag}: {e}")
            break

        nuevos = parsear_productos_html(r.text)

        if not nuevos:
            print(f"  ✅ Sin productos en PASE={pase}, fin de categoría")
            break

        # Si todos los IDs ya estaban, la web está repitiendo → paramos
        ids_realmente_nuevos = set(nuevos.keys()) - set(productos.keys())
        if not ids_realmente_nuevos:
            print(f"  ⚠️  La web repite productos en PASE={pase}, fin de categoría")
            break

        productos.update(nuevos)
        print(f"    PASE={pase}: {len(ids_realmente_nuevos)} nuevos (Total: {len(productos)})")

        pase += 1
        time.sleep(1)

        if pase > 200:
            print("  ⚠️  Límite de 200 páginas alcanzado")
            break

    return productos


def scrape_pagina_oferta(numero):
    """Intenta descargar oferta{numero}.html. Devuelve dict o None si no existe."""
    url = f"{OFERTA_BASE_URL}{numero}.html"
    try:
        r = session.get(url, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
    except requests.exceptions.HTTPError:
        return None
    except Exception as e:
        print(f"  ⚠️  Error en {url}: {e}")
        return None

    productos = parsear_productos_html(r.text)
    if not productos:
        return None
    return {"url": url, "productos": productos}


# ── Notificaciones ────────────────────────────────────────────────────

def enviar_telegram(mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️  Sin credenciales Telegram — volcando por consola:")
        print("─" * 60)
        print(mensaje)
        print("─" * 60)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    print(f"  📤 Enviando a Telegram...")
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        print("  ✅ Enviado")
    except Exception as e:
        print(f"  ❌ Error Telegram: {e}")


def comparar_y_notificar(nombre_cat, productos_nuevos, productos_anteriores):
    mensajes = []

    # 1. Productos NUEVOS
    nuevos = {k: v for k, v in productos_nuevos.items() if k not in productos_anteriores}
    if nuevos:
        lista = "\n".join(
            f"  • <a href='{p['url']}'>{p['nombre']}</a> — {p['precio']}"
            for p in list(nuevos.values())[:10]
        )
        extra = f"\n  <i>...y {len(nuevos) - 10} más</i>" if len(nuevos) > 10 else ""
        mensajes.append(f"🆕 <b>Nuevos productos en {nombre_cat}</b>\n{lista}{extra}")

    # 2. Productos ELIMINADOS (solo si no son demasiados)
    eliminados = {k: v for k, v in productos_anteriores.items() if k not in productos_nuevos}
    if 0 < len(eliminados) < 20:
        lista = "\n".join(f"  • {p['nombre']}" for p in list(eliminados.values())[:5])
        extra = f"\n  <i>...y {len(eliminados) - 5} más</i>" if len(eliminados) > 5 else ""
        mensajes.append(f"❌ <b>Eliminados en {nombre_cat}</b>\n{lista}{extra}")

    # 3. Cambios de PRECIO
    cambios = []
    for k, prod_nuevo in productos_nuevos.items():
        if k in productos_anteriores:
            p_ant = productos_anteriores[k].get("precio", "")
            p_nue = prod_nuevo.get("precio", "")
            if p_ant and p_nue and p_ant != p_nue:
                cambios.append(
                    f"  • <a href='{prod_nuevo['url']}'>{prod_nuevo['nombre']}</a>: "
                    f"{p_ant} → <b>{p_nue}</b>"
                )
    if cambios:
        lista = "\n".join(cambios[:10])
        extra = f"\n  <i>...y {len(cambios) - 10} más</i>" if len(cambios) > 10 else ""
        mensajes.append(f"💸 <b>Cambios de precio en {nombre_cat}</b>\n{lista}{extra}")

    return mensajes


# ── Main ───────────────────────────────────────────────────────────────

def main():
    print(f"\n🕐 Monitor perfumedigital.es — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    estado_anterior = cargar_estado()
    estado_nuevo    = {}
    todos_mensajes  = []

    # ── 1. Categorías fijas ────────────────────────────────────────────
    for cat in CATEGORIAS:
        nombre = cat["nombre"]
        url    = cat["url"]
        print(f"\n📦 Scrapeando {nombre}...")

        productos  = scrape_categoria(url)
        anteriores = estado_anterior.get(url, {})
        print(f"  → {len(productos)} productos encontrados")

        estado_nuevo[url] = productos

        if anteriores:
            msgs = comparar_y_notificar(nombre, productos, anteriores)
            todos_mensajes.extend(msgs)
        else:
            print("  ℹ️  Primera ejecución, guardando estado inicial")

    # ── 2. Páginas de oferta numeradas ─────────────────────────────────
    print(f"\n🔢 Comprobando páginas de oferta numeradas...")

    ultimo = estado_anterior.get(OFERTA_NUMERO_KEY, OFERTA_INICIO)
    estado_nuevo[OFERTA_NUMERO_KEY] = ultimo  # por defecto no cambia

    # 2a. Revisamos la oferta actual (puede haber cambiado sus productos)
    print(f"  🔍 Oferta actual: oferta{ultimo}.html")
    resultado_actual = scrape_pagina_oferta(ultimo)

    if resultado_actual:
        url_act   = resultado_actual["url"]
        prods_act = resultado_actual["productos"]
        print(f"  → {len(prods_act)} productos")
        estado_nuevo[url_act] = prods_act
        anteriores_act = estado_anterior.get(url_act, {})

        if anteriores_act:
            msgs = comparar_y_notificar(f"🏷️ Oferta {ultimo}", prods_act, anteriores_act)
            todos_mensajes.extend(msgs)
        else:
            print(f"  ℹ️  Primera vez que vemos oferta{ultimo}, guardando estado inicial")

    # 2b. Comprobamos si ya existe la siguiente
    siguiente = ultimo + 1
    print(f"  🔍 Buscando nueva: oferta{siguiente}.html...")
    resultado_sig = scrape_pagina_oferta(siguiente)

    if resultado_sig:
        url_sig   = resultado_sig["url"]
        prods_sig = resultado_sig["productos"]
        print(f"  🎉 ¡Nueva página detectada! {len(prods_sig)} productos")
        estado_nuevo[url_sig] = prods_sig
        estado_nuevo[OFERTA_NUMERO_KEY] = siguiente  # actualizamos el contador

        lista = "\n".join(
            f"  • <a href='{p['url']}'>{p['nombre']}</a> — {p['precio']}"
            for p in list(prods_sig.values())[:15]
        )
        extra = f"\n  <i>...y {len(prods_sig) - 15} más</i>" if len(prods_sig) > 15 else ""
        todos_mensajes.append(
            f"🚨 <b>¡NUEVA PÁGINA DE OFERTAS!</b>\n"
            f"<a href='{url_sig}'>perfumedigital.es/oferta{siguiente}.html</a> "
            f"— {len(prods_sig)} productos\n\n{lista}{extra}"
        )
    else:
        print(f"  ✅ oferta{siguiente}.html aún no existe")

    # ── 3. Enviar ────────────────────────────────────────────────────
    if todos_mensajes:
        print(f"\n📣 {len(todos_mensajes)} notificaciones")
        for msg in todos_mensajes:
            enviar_telegram(msg)
    else:
        print("\n✅ Sin cambios detectados")

    guardar_estado(estado_nuevo)
    print("\n💾 Estado guardado\n")


if __name__ == "__main__":
    main()