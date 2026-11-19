"""Panel local de reservas. Ejecutar con Python 3.10+."""
import calendar
import html
import re
import secrets
import sqlite3
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

PROPIEDADES = ("Zafarrancho Zárate", "La Porteña")
BASE = Path(__file__).with_name("reservas.sqlite3")
TOKEN = secrets.token_urlsafe(32)
MESES = "Enero Febrero Marzo Abril Mayo Junio Julio Agosto Septiembre Octubre Noviembre Diciembre".split()
esc = html.escape


def conectar():
    db = sqlite3.connect(BASE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def inicializar(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS reservas (
            id INTEGER PRIMARY KEY, propiedad INTEGER NOT NULL,
            nombre TEXT NOT NULL, telefono TEXT NOT NULL,
            ingreso TEXT NOT NULL, salida TEXT NOT NULL,
            personas INTEGER NOT NULL, total INTEGER NOT NULL,
            vence TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pagos (
            id INTEGER PRIMARY KEY, reserva INTEGER NOT NULL REFERENCES reservas(id),
            fecha TEXT NOT NULL, importe INTEGER NOT NULL
        );
    """)


def pesos(valor):
    return "$" + f"{valor // 100:,}".replace(",", ".") + f",{valor % 100:02}"


def importe(valor):
    if not re.fullmatch(r"[0-9]{1,12}(?:[,.][0-9]{1,2})?", valor):
        raise ValueError("Ingresá el importe sin separador de miles, por ejemplo 150000,50.")
    entero, _, decimal = valor.replace(",", ".").partition(".")
    resultado = int(entero) * 100 + int(decimal.ljust(2, "0"))
    if resultado <= 0:
        raise ValueError("El importe debe ser mayor que cero.")
    return resultado


def guardar(db, datos):
    db.execute("BEGIN IMMEDIATE")
    if datos["accion"] == "reserva":
        propiedad = int(datos["propiedad"])
        ingreso, salida, vence = [
            date.fromisoformat(datos[campo]).isoformat()
            for campo in ("ingreso", "salida", "vence")
        ]
        nombre, telefono = datos["nombre"].strip(), datos["telefono"].strip()
        personas = int(datos["personas"])
        if propiedad not in range(len(PROPIEDADES)) or not nombre or not telefono or personas < 1:
            raise ValueError("Completá los datos de la reserva.")
        if salida <= ingreso:
            raise ValueError("La salida debe ser posterior al ingreso.")
        cruce = db.execute(
            "SELECT id FROM reservas WHERE propiedad=? AND ingreso<? AND salida>?",
            (propiedad, salida, ingreso),
        ).fetchone()
        if cruce:
            raise ValueError(f"Esas fechas se superponen con la reserva #{cruce['id']}.")
        db.execute(
            "INSERT INTO reservas VALUES (NULL,?,?,?,?,?,?,?,?)",
            (propiedad, nombre, telefono, ingreso, salida, personas, importe(datos["total"]), vence),
        )
        return propiedad
    if datos["accion"] != "pago":
        raise ValueError("Operación inválida.")
    reserva = db.execute(
        """SELECT r.propiedad, r.total - COALESCE(SUM(p.importe),0) AS saldo
           FROM reservas r LEFT JOIN pagos p ON p.reserva=r.id
           WHERE r.id=? GROUP BY r.id""",
        (int(datos["reserva"]),),
    ).fetchone()
    monto = importe(datos["importe"])
    if not reserva or monto > reserva["saldo"]:
        raise ValueError("El pago supera el saldo pendiente o la reserva no existe.")
    fecha = date.fromisoformat(datos["fecha"]).isoformat()
    db.execute("INSERT INTO pagos VALUES (NULL,?,?,?)", (int(datos["reserva"]), fecha, monto))
    return reserva["propiedad"]


def campo(etiqueta, nombre, tipo="text", valor="", atributos=""):
    return (
        f'<label>{etiqueta}<input required name="{nombre}" type="{tipo}" '
        f'value="{esc(valor)}" {atributos}></label>'
    )


def formulario(accion, contenido):
    return (
        f'<form method="post"><input type="hidden" name="token" value="{TOKEN}">'
        f'<input type="hidden" name="accion" value="{accion}">{contenido}</form>'
    )


def mostrar_fecha(valor):
    return date.fromisoformat(valor).strftime("%d/%m/%Y")


def pagina(db, propiedad, mes, error=""):
    hoy = date.today().isoformat()
    reservas = db.execute(
        """SELECT r.*, COALESCE(SUM(p.importe),0) AS abonado
           FROM reservas r LEFT JOIN pagos p ON p.reserva=r.id
           WHERE r.propiedad=? GROUP BY r.id ORDER BY r.ingreso""",
        (propiedad,),
    ).fetchall()
    historial = {}
    for pago in db.execute(
        """SELECT p.* FROM pagos p JOIN reservas r ON r.id=p.reserva
           WHERE r.propiedad=? ORDER BY p.fecha,p.id""", (propiedad,)
    ):
        historial.setdefault(pago["reserva"], []).append(pago)

    nombre_prop = PROPIEDADES[propiedad]
    selector = " · ".join(
        f'<a href="/?propiedad={i}">{esc(nombre)}</a>'
        for i, nombre in enumerate(PROPIEDADES)
    )
    calendario = []
    for semana in calendar.Calendar().monthdatescalendar(mes.year, mes.month):
        for dia in semana:
            if dia.month != mes.month:
                calendario.append('<span class="dia"></span>')
                continue
            fecha = dia.isoformat()
            ocupada = any(r["ingreso"] <= fecha < r["salida"] for r in reservas)
            clase = "ocupado" if ocupada else "libre"
            estado = "Ocupado" if ocupada else "Disponible"
            calendario.append(f'<span class="dia {clase}" aria-label="{dia.day} {estado}">{dia.day}</span>')
    encabezados = "".join(f"<b>{d}</b>" for d in ("Lu", "Ma", "Mi", "Ju", "Vi", "Sá", "Do"))
    eventos = []
    for r in reservas:
        for clave, etiqueta in (("ingreso", "Ingreso"), ("salida", "Salida")):
            if r[clave] >= hoy:
                eventos.append((r[clave], etiqueta, r))
    eventos.sort(key=lambda evento: (evento[0], evento[1], evento[2]["id"]))
    proximos = "".join(
        f'<li>{mostrar_fecha(fecha)} · {etiqueta} · <a href="#r{r["id"]}">{esc(r["nombre"])}</a></li>'
        for fecha, etiqueta, r in eventos[:8]
    ) or "<li>Sin movimientos próximos.</li>"
    alertas = []
    fichas = []
    for r in reservas:
        identificador = r["id"]
        saldo = r["total"] - r["abonado"]
        nombre = esc(r["nombre"])
        vence = mostrar_fecha(r["vence"])
        pagos = "".join(
            f'<li>{mostrar_fecha(p["fecha"])} · {pesos(p["importe"])}</li>'
            for p in historial.get(identificador, [])
        ) or "<li>Todavía no hay pagos registrados.</li>"
        acciones = ""
        if saldo:
            if r["vence"] <= hoy:
                estado = "Vence hoy" if r["vence"] == hoy else "Vencido"
                alertas.append(f'<li>{estado} · <a href="#r{identificador}">{nombre}</a> · {pesos(saldo)}</li>')
            acciones = formulario(
                "pago",
                f'<input type="hidden" name="reserva" value="{identificador}">'
                + campo("Importe en ARS", "importe", atributos='inputmode="decimal"')
                + campo("Fecha del pago", "fecha", "date", hoy)
                + '<button>Registrar pago</button>',
            )
            mensaje = (
                f"Hola, {r['nombre']}. Te escribo por tu reserva en {nombre_prop}. "
                f"El saldo pendiente es de {pesos(saldo)} y la fecha de vencimiento es el {vence}. "
                "Cuando hagas la transferencia, ¿me enviás el comprobante? Gracias."
            )
            acciones += (
                f'<label>Recordatorio para WhatsApp<textarea readonly>{esc(mensaje)}</textarea></label>'
                '<button type="button" class="copiar">Copiar recordatorio</button>'
            )
        fichas.append(f"""
            <article id="r{identificador}"><h3>#{identificador} · {nombre}</h3>
            <p>{mostrar_fecha(r['ingreso'])} al {mostrar_fecha(r['salida'])} · {r['personas']} huéspedes
            · {esc(r['telefono'])}</p>
            <p>Total {pesos(r['total'])} · Abonado {pesos(r['abonado'])} · <strong>Saldo {pesos(saldo)}</strong></p>
            <p>Vencimiento del saldo {vence}</p><details><summary>Historial de pagos</summary>
            <ul>{pagos}</ul></details>{acciones}</article>
        """)
    alta = formulario(
        "reserva", f'<input type="hidden" name="propiedad" value="{propiedad}">'
        + campo("Nombre", "nombre", atributos='maxlength="120"')
        + campo("Teléfono", "telefono", "tel", atributos='maxlength="50"')
        + campo("Ingreso", "ingreso", "date")
        + campo("Salida", "salida", "date")
        + campo("Huéspedes", "personas", "number", "1", 'min="1"')
        + campo("Total en ARS", "total", atributos='inputmode="decimal"')
        + campo("Vencimiento del saldo", "vence", "date")
        + '<button>Guardar reserva</button>',
    )
    aviso = f'<p role="alert" class="error">{esc(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="es"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Panel de reservas</title><style>
    *{{box-sizing:border-box}}body{{font:16px system-ui,sans-serif;background:#f5f3ef;color:#24342e;
    margin:auto;max-width:960px;padding:24px}}a{{color:#265f4c}}section,article{{background:white;
    border-radius:14px;padding:20px;margin:16px 0;border:1px solid #e4e8e0}}article{{background:#fafbf8}}
    form{{display:flex;gap:12px;flex-wrap:wrap;align-items:end;margin:14px 0}}
    label{{display:grid;gap:5px}}input,textarea,button{{font:inherit;border:1px solid #a6b7ae;
    border-radius:8px;padding:8px;color:inherit}}textarea{{width:100%;min-height:90px}}
    button{{background:#265f4c;color:white;cursor:pointer}}.copiar{{margin-top:8px}}
    .grilla{{display:grid;grid-template-columns:repeat(7,1fr);gap:4px;text-align:center}}
    .dia{{padding:6px;border-radius:6px}}.ocupado{{background:#cce4d5}}.libre{{background:#f2f2ec}}
    .error{{background:#ffe3dc;padding:12px;border-radius:8px}}
    </style><header><p>Panel de reservas · Pesos argentinos</p><h1>{esc(nombre_prop)}</h1>
    <nav>{selector}</nav></header>{aviso}
    <section><h2>Próximos ingresos y salidas</h2><ul>{proximos}</ul>
    <h2>Saldos de hoy y vencidos</h2><ul>{''.join(alertas) or '<li>Sin saldos para reclamar.</li>'}</ul></section>
    <section><h2>Calendario · {MESES[mes.month-1]} {mes.year}</h2>
    <form method="get"><input type="hidden" name="propiedad" value="{propiedad}">
    <label>Mes<input type="month" name="mes" value="{mes.isoformat()[:7]}" required></label>
    <button>Ver mes</button></form><p>Verde = ocupado · Gris = disponible. El día de salida queda disponible.</p>
    <div class="grilla">{encabezados}{''.join(calendario)}</div></section>
    <section><h2>Nueva reserva</h2>{alta}</section>
    <section><h2>Reservas y pagos</h2>{''.join(fichas) or '<p>Todavía no hay reservas.</p>'}</section>
    <script>
    document.querySelectorAll('.copiar').forEach(boton => boton.onclick = async () => {{
      const texto = boton.previousElementSibling.querySelector('textarea');
      try {{ await navigator.clipboard.writeText(texto.value); boton.textContent = 'Copiado'; }}
      catch {{ texto.focus(); texto.select(); boton.textContent = 'Texto seleccionado. Copialo manualmente'; }}
    }});
    </script></html>"""


class Panel(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlsplit(self.path)
        if url.path != "/":
            self.send_error(404)
            return
        consulta = parse_qs(url.query)
        try:
            propiedad = int(consulta.get("propiedad", ["0"])[0])
            mes = date.fromisoformat(consulta.get("mes", [date.today().isoformat()[:7]])[0] + "-01")
            if propiedad not in range(len(PROPIEDADES)) or not 2 <= mes.year <= 9998:
                raise ValueError
        except ValueError:
            self.send_error(400, "Fecha o alojamiento incorrecto")
            return
        self.renderizar(propiedad, mes)

    def do_POST(self):
        if urlsplit(self.path).path != "/":
            self.send_error(404)
            return
        try:
            longitud = int(self.headers.get("Content-Length", "0"))
            if not 0 < longitud <= 8192:
                raise ValueError
            datos = {
                k: v[0] for k, v in parse_qs(
                    self.rfile.read(longitud).decode(), keep_blank_values=True
                ).items()
            }
        except ValueError:
            self.send_error(400)
            return
        if not secrets.compare_digest(datos.get("token", "").encode(), TOKEN.encode()):
            self.send_error(403)
            return
        db = conectar()
        try:
            with db:
                propiedad = guardar(db, datos)
        except (ValueError, KeyError, OverflowError) as error:
            consulta = parse_qs(urlsplit(self.path).query)
            valor = consulta.get("propiedad", ["0"])[0]
            propiedad = int(valor) if valor in ("0", "1") else 0
            self.renderizar(propiedad, date.today().replace(day=1), str(error), 400)
            return
        finally:
            db.close()
        self.send_response(303)
        self.send_header("Location", "/?" + urlencode({"propiedad": propiedad}))
        self.end_headers()

    def renderizar(self, propiedad, mes, error="", estado=200):
        db = conectar()
        try:
            cuerpo = pagina(db, propiedad, mes, error).encode()
        finally:
            db.close()
        self.send_response(estado)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)


if __name__ == "__main__":
    db = conectar()
    try:
        inicializar(db)
    finally:
        db.close()
    with HTTPServer(("0.0.0.0", 8000), Panel) as servidor:
        print("Abrí http://127.0.0.1:8000 en tu navegador. Cerrá con Ctrl+C.")
        try:
            servidor.serve_forever()
        except KeyboardInterrupt:
            pass
