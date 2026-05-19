#!/usr/bin/env python3
"""
Monitor de perfumedigital.es
- Monitoriza categorías fijas (paginadas por PASE)
- Detecta automáticamente nuevas páginas de oferta numeradas (oferta815, oferta816...)
- Notifica por Telegram con protección anti-spam y anti-crash
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
TELEGRAM_THREAD_ID = os.environ.get("TELEGRAM_THREAD_ID", "")

BASE_URL = "https://perfumedigital.es"

# Categorías fijas a monitorizar
CATEGORIAS = [
    {
        "nombre": "👨 Perfumes de Hombre",
        "url": f"{BASE_URL}/index.php?ID_CATEGORIA=e5cb35910ddb33ceee5124e79cf89c93",
    },
    {
        "nombre": "👩 Perfumes de Mujer",
        "url": f"{BASE_URL}/index.php?ID_CATEGORIA=f7bbbba04e61534f795c9c6c5e5affee",
    },
    {
        "nombre": "🏷️ Outlet Perfumería",
        "url": f"{BASE_URL}/index.php?ID_CATEGORIA=categoria_outlet",
    },
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
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            print(f"  ⚠️ Archivo de estado corrupto ignorado ({e}). Empezando de cero.")
            return {}
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
    """Descarga todos los productos saltando de 15 en 15 (offset)"""
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

        # Filtramos los que ya tenemos
        ids_realmente_nuevos = set(nuevos.keys()) - set(productos.keys())
        
        if not ids_realmente_nuevos:
            print(f"  ⚠️  La web repite productos en PASE={pase}, fin de categoría")
            break

        productos.update(nuevos)
        print(f"    PASE={pase}: {len(ids_realmente_nuevos)} nuevos (Total: {len(productos)})")

        # ¡EL SECRETO ESTABA AQUÍ!
        # Saltamos de 15 en 15 productos, que es el tamaño real de cada página
        pase += 15
        time.sleep(1)

        # Límite de seguridad: 6000 productos (equivale a PASE=6000)
        if pase > 6000:
            print("  ⚠️  Límite de seguridad de 6000 productos alcanzado")
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
    
    # Límite seguro de Telegram
    limite_caracteres = 4000
    mensajes_cortados = []
    
    # Lógica para dividir mensajes largos sin romper HTML
    if len(mensaje) <= limite_caracteres:
        mensajes_cortados.append(mensaje)
    else:
        lineas = mensaje.split('\n')
        bloque_actual = ""
        for linea in lineas:
            if len(bloque_actual) + len(linea) + 1 > limite_caracteres:
                mensajes_cortados.append(bloque_actual.strip())
                bloque_actual = linea + "\n"
            else:
                bloque_actual += linea + "\n"
        if bloque_actual:
            mensajes_cortados.append(bloque_actual.strip())

    # Enviar cada bloque con control de Anti-Spam (Error 429)
    for i, msg in enumerate(mensajes_cortados):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        
        if TELEGRAM_THREAD_ID:
            payload["message_thread_id"] = int(TELEGRAM_THREAD_ID)
            
        print(f"  📤 Enviando bloque {i+1}/{len(mensajes_cortados)} a Telegram...")
        
        max_reintentos = 3
        for intento in range(max_reintentos):
            try:
                r = requests.post(url, json=payload, timeout=15)
                
                # Si Telegram nos bloquea temporalmente (Error 429)
                if r.status_code == 429:
                    espera = r.json().get("parameters", {}).get("retry_after", 5)
                    print(f"  ⏳ Telegram pide frenar. Esperando {espera} segundos...")
                    time.sleep(espera + 1)
                    continue  # Volvemos a intentar enviar el mismo bloque
                    
                r.raise_for_status()
                print("  ✅ Enviado")
                break  # Éxito, salimos del bucle de reintentos
                
            except Exception as e:
                print(f"  ❌ Error Telegram: {e}")
                break  # Si es otro tipo de error, cancelamos el envío de este bloque
        
        # Pausa de 3.5 segundos entre bloques (límite de Telegram: 20 msgs / minuto)
        time.sleep(3.5)


def comparar_y_notificar(nombre_cat, productos_nuevos, productos_anteriores, ya_notificados=None):
    mensajes = []
    if ya_notificados is None:
        ya_notificados = set()

    # Límite para agrupar notificaciones (evita spam masivo en Telegram)
    LIMITE_DETALLE = 20

    # 1. Productos NUEVOS (filtrando los que ya se notificaron en otra categoría)
    nuevos = {k: v for k, v in productos_nuevos.items()
              if k not in productos_anteriores and v['nombre'] not in ya_notificados}
    if nuevos:
        # Registrar como ya notificados para las siguientes categorías
        for p in nuevos.values():
            ya_notificados.add(p['nombre'])

        if len(nuevos) <= LIMITE_DETALLE:
            lista = "\n".join(
                f"  • <a href='{p['url']}'>{p['nombre']}</a> — {p['precio']}"
                for p in nuevos.values()
            )
            mensajes.append(f"🆕 <b>Nuevos productos en {nombre_cat}</b>\n{lista}")
        else:
            # Demasiados → resumen compacto (probablemente la web se recuperó de un fallo)
            muestra = list(nuevos.values())[:5]
            lista_muestra = "\n".join(
                f"  • <a href='{p['url']}'>{p['nombre']}</a> — {p['precio']}"
                for p in muestra
            )
            mensajes.append(
                f"🆕 <b>{len(nuevos)} nuevos productos en {nombre_cat}</b>\n"
                f"(Mostrando 5 de {len(nuevos)}):\n{lista_muestra}\n"
                f"  ...y {len(nuevos) - 5} más"
            )



    # 3. Cambios de PRECIO (filtrando ya notificados en otra categoría)
    cambios = []
    for k, prod_nuevo in productos_nuevos.items():
        if k in productos_anteriores:
            # Si ya se notificó este producto en otra categoría, saltar
            if prod_nuevo['nombre'] in ya_notificados:
                continue

            p_ant = productos_anteriores[k].get("precio", "")
            p_nue = prod_nuevo.get("precio", "")
            if p_ant and p_nue and p_ant != p_nue:
                cambios.append(
                    f"  • <a href='{prod_nuevo['url']}'>{prod_nuevo['nombre']}</a>: "
                    f"{p_ant} → <b>{p_nue}</b>"
                )
                ya_notificados.add(prod_nuevo['nombre'])
    if cambios:
        if len(cambios) <= LIMITE_DETALLE:
            lista = "\n".join(cambios)
            mensajes.append(f"💸 <b>Cambios de precio en {nombre_cat}</b>\n{lista}")
        else:
            lista = "\n".join(cambios[:10])
            mensajes.append(
                f"💸 <b>{len(cambios)} cambios de precio en {nombre_cat}</b>\n"
                f"(Mostrando 10 de {len(cambios)}):\n{lista}\n"
                f"  ...y {len(cambios) - 10} más"
            )

    return mensajes


# ── Main ───────────────────────────────────────────────────────────────

def main():
    print(f"\n🕐 Monitor perfumedigital.es — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    estado_anterior = cargar_estado()
    estado_nuevo    = {}
    todos_mensajes  = []
    ya_notificados  = set()  # Evita notificar el mismo producto en varias categorías

    # ── 1. Categorías fijas ────────────────────────────────────────────
    for cat in CATEGORIAS:
        nombre = cat["nombre"]
        url    = cat["url"]
        print(f"\n📦 Scrapeando {nombre}...")

        productos  = scrape_categoria(url)
        anteriores = estado_anterior.get(url, {})
        print(f"  → {len(productos)} productos encontrados")

        # ── PROTECCIÓN ANTI-SCRAPING-FALLIDO ──────────────────────
        # Si la categoría antes tenía productos y ahora devuelve muchos menos
        # (menos del 80%), probablemente la web falló o nos bloqueó.
        # En ese caso, MANTENEMOS el estado anterior para no generar
        # falsas notificaciones de "eliminados" y luego "nuevos".
        if anteriores and len(productos) < len(anteriores) * 0.8:
            print(f"  ⚠️  PROTECCIÓN: Se esperaban ~{len(anteriores)} productos pero solo se obtuvieron {len(productos)}.")
            print(f"  ⚠️  Esto indica un fallo de la web, NO un cambio real. Se mantiene el estado anterior.")
            estado_nuevo[url] = anteriores  # Mantener estado anterior
            continue

        # ── PROTECCIÓN ANTI-RECUPERACIÓN ──────────────────────────
        # Si de repente aparecen muchos productos "nuevos" (más de 30),
        # es probable que el scrape ANTERIOR fue parcial y ahora se
        # recuperó. Actualizamos el estado SIN notificar.
        if anteriores:
            nuevos_detectados = set(productos.keys()) - set(anteriores.keys())
            if len(nuevos_detectados) > 30:
                print(f"  ⚠️  PROTECCIÓN ANTI-RECUPERACIÓN: Se detectaron {len(nuevos_detectados)} productos 'nuevos'.")
                print(f"  ⚠️  Probablemente el scrape anterior fue parcial. Se actualiza estado SIN notificar.")
                estado_nuevo[url] = productos
                continue

        estado_nuevo[url] = productos

        if anteriores:
            msgs = comparar_y_notificar(nombre, productos, anteriores, ya_notificados)
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
            msgs = comparar_y_notificar(f"🏷️ Oferta {ultimo}", prods_act, anteriores_act, ya_notificados)
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

        # ¡Nueva lista sin recortes!
        lista = "\n".join(
            f"  • <a href='{p['url']}'>{p['nombre']}</a> — {p['precio']}"
            for p in prods_sig.values()
        )
        todos_mensajes.append(
            f"🚨 <b>¡NUEVA PÁGINA DE OFERTAS!</b>\n"
            f"<a href='{url_sig}'>perfumedigital.es/oferta{siguiente}.html</a> "
            f"— {len(prods_sig)} productos\n\n{lista}"
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
