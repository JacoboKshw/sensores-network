from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import sqlite3
import psycopg2
from psycopg2.extras import RealDictCursor
from pathlib import Path
from datetime import datetime
import asyncio
import json
import threading
import websockets
import os

# -----------------------
# CONFIGURACIÓN
# -----------------------
WS_PORT = int(os.environ.get("WS_PORT", 9001))
PORT = int(os.environ.get("PORT", 5000))

# DATABASE: Detectar si estamos en producción (Render) o desarrollo (local)
DATABASE_URL = os.environ.get("DATABASE_URL")  # Supabase o Render lo proporcionan
USE_POSTGRES = DATABASE_URL is not None

if USE_POSTGRES:
    print("🐘 Usando PostgreSQL (Supabase/Producción)")
else:
    print("🗄️  Usando SQLite (Desarrollo local)")
    DB_PATH = Path("sensors.db")

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

# -----------------------
# DB helpers - VERSIÓN DUAL (SQLite + PostgreSQL)
# -----------------------
def get_db():
    """Retorna conexión a la base de datos (SQLite o PostgreSQL según entorno)"""
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
        return conn
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

def init_db():
    """Inicializa las tablas en la base de datos"""
    conn = get_db()
    cur = conn.cursor()
    
    if USE_POSTGRES:
        # Sintaxis para PostgreSQL
        cur.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                id SERIAL PRIMARY KEY,
                node_name TEXT NOT NULL UNIQUE,
                lat REAL DEFAULT 0.0,
                lng REAL DEFAULT 0.0,
                last_seen TIMESTAMP,
                online INTEGER DEFAULT 1
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id SERIAL PRIMARY KEY,
                node_id INTEGER REFERENCES nodes(id),
                timestamp TIMESTAMP NOT NULL,
                soil_moisture REAL,
                temperature REAL,
                humidity REAL,
                lux REAL
            );
        """)
    else:
        # Sintaxis para SQLite
        cur.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_name TEXT NOT NULL UNIQUE,
                lat REAL DEFAULT 0.0,
                lng REAL DEFAULT 0.0,
                last_seen TEXT,
                online INTEGER DEFAULT 1
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id INTEGER,
                timestamp TEXT NOT NULL,
                soil_moisture REAL,
                temperature REAL,
                humidity REAL,
                lux REAL,
                FOREIGN KEY(node_id) REFERENCES nodes(id)
            );
        """)
    
    conn.commit()
    conn.close()
    print("✅ Base de datos inicializada correctamente")

# -----------------------
# Guardar lectura y emitir a front-end
# -----------------------
def handle_new_reading(data):
    """Procesa una nueva lectura de sensor y la guarda en la BD"""
    node_name = data.get("nodeID")
    if not node_name:
        raise ValueError("Missing 'nodeID' in reading data.")

    conn = get_db()
    cur = conn.cursor()

    try:
        # 1. Asegurar que el nodo exista (o crearlo)
        if USE_POSTGRES:
            cur.execute("SELECT id FROM nodes WHERE node_name = %s", (node_name,))
        else:
            cur.execute("SELECT id FROM nodes WHERE node_name = ?", (node_name,))
        
        node = cur.fetchone()

        if not node:
            lat = data.get("lat", 0.0)
            lng = data.get("lng", 0.0)
            
            if USE_POSTGRES:
                cur.execute("""
                    INSERT INTO nodes (node_name, lat, lng, last_seen, online) 
                    VALUES (%s, %s, %s, %s, 1) RETURNING id
                """, (node_name, lat, lng, datetime.now()))
                node_id = cur.fetchone()['id']
            else:
                cur.execute("""
                    INSERT INTO nodes (node_name, lat, lng, last_seen, online) 
                    VALUES (?, ?, ?, ?, 1)
                """, (node_name, lat, lng, datetime.now().isoformat()))
                node_id = cur.lastrowid
        else:
            node_id = node["id"]
            
            if USE_POSTGRES:
                cur.execute("""
                    UPDATE nodes SET last_seen = %s, online = 1, lat = %s, lng = %s 
                    WHERE id = %s
                """, (datetime.now(), data.get("lat", 0.0), data.get("lng", 0.0), node_id))
            else:
                cur.execute("""
                    UPDATE nodes SET last_seen = ?, online = 1, lat = ?, lng = ? 
                    WHERE id = ?
                """, (datetime.now().isoformat(), data.get("lat", 0.0), data.get("lng", 0.0), node_id))

        # 2. Guardar la lectura
        if USE_POSTGRES:
            cur.execute("""
                INSERT INTO readings (node_id, timestamp, soil_moisture, temperature, humidity, lux)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                node_id,
                datetime.now(),
                data.get("humedad_suelo"),
                data.get("temperatura"),
                data.get("humedad_ambiente"),
                data.get("radiacion_solar")
            ))
        else:
            cur.execute("""
                INSERT INTO readings (node_id, timestamp, soil_moisture, temperature, humidity, lux)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                node_id,
                datetime.now().isoformat(),
                data.get("humedad_suelo"),
                data.get("temperatura"),
                data.get("humedad_ambiente"),
                data.get("radiacion_solar")
            ))

        conn.commit()
        print(f"✅ Lectura guardada - Nodo: {node_name}")
        
    except Exception as e:
        print(f"❌ Error guardando lectura: {e}")
        raise
    finally:
        conn.close()
    
    # 3. Emitir actualización a Socket.IO para el front-end
    try:
        update_data = fetch_latest_data()
        socketio.emit('new_reading', update_data)
    except Exception as e:
        print(f"⚠️ Error emitiendo datos a Socket.IO: {e}")

# -----------------------
# Flask Routes (HTTP)
# -----------------------
@app.route('/')
def index():
    """Renderiza el dashboard principal"""
    return render_template('index.html')

@app.route('/api/latest', methods=['GET'])
def get_latest():
    """Endpoint para obtener la última lectura"""
    try:
        data = fetch_latest_data()
        return jsonify(data.get('latest', {}))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/history', methods=['GET'])
def get_history():
    """Endpoint para obtener históricos"""
    try:
        data = fetch_latest_data()
        return jsonify(data.get('history', {}))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/nodes', methods=['GET'])
def get_nodes():
    """Endpoint para obtener información de nodos"""
    try:
        data = fetch_latest_data()
        return jsonify(data.get('nodes', []))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/data', methods=['GET'])
def get_data():
    """Endpoint completo con todos los datos"""
    try:
        data = fetch_latest_data()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check para Render y monitoreo"""
    return jsonify({
        "status": "healthy", 
        "timestamp": datetime.now().isoformat(),
        "database": "PostgreSQL" if USE_POSTGRES else "SQLite",
        "port": PORT
    })

# -----------------------
# Socket.IO (Front-end live updates)
# -----------------------
@socketio.on('connect')
def handle_connect():
    print('✅ Cliente conectado via Socket.IO')
    try:
        data = fetch_latest_data()
        socketio.emit('new_reading', data)
    except Exception as e:
        print(f"❌ Error al enviar datos iniciales: {e}")

@socketio.on('disconnect')
def handle_disconnect():
    print('⚠️ Cliente desconectado de Socket.IO')

# -----------------------
# Data Fetchers (para front-end)
# -----------------------
def fetch_latest_data():
    """Obtiene los datos más recientes de la base de datos"""
    conn = get_db()
    cur = conn.cursor()

    try:
        cur.execute("""
            SELECT 
                r.*, 
                n.node_name, 
                n.lat, 
                n.lng 
            FROM readings r 
            JOIN nodes n ON r.node_id = n.id 
            WHERE r.id IN (
                SELECT MAX(id) FROM readings GROUP BY node_id
            ) 
            ORDER BY r.timestamp DESC;
        """)
        latest_readings = [dict(row) for row in cur.fetchall()]

        cur.execute("SELECT node_name, lat, lng, online FROM nodes;")
        all_nodes = [dict(row) for row in cur.fetchall()]
        
        latest_reading_overall = latest_readings[0] if latest_readings else {
            "timestamp": None,
            "soil_moisture": None,
            "temperature": None,
            "humidity": None,
            "lux": None
        }
        
        history_data = {"labels": [], "temperature": [], "humidity": [], "soil_moisture": []}
        
        if latest_readings and latest_reading_overall.get('node_id'):
            target_node_id = latest_reading_overall['node_id']
            
            if USE_POSTGRES:
                cur.execute("""
                    SELECT timestamp, temperature, humidity, soil_moisture 
                    FROM readings 
                    WHERE node_id = %s 
                    ORDER BY timestamp DESC 
                    LIMIT 50;
                """, (target_node_id,))
            else:
                cur.execute("""
                    SELECT timestamp, temperature, humidity, soil_moisture 
                    FROM readings 
                    WHERE node_id = ? 
                    ORDER BY timestamp DESC 
                    LIMIT 50;
                """, (target_node_id,))
            
            history = cur.fetchall()
            history.reverse()

            for row in history:
                if USE_POSTGRES:
                    dt_obj = row['timestamp']
                else:
                    dt_obj = datetime.fromisoformat(row['timestamp'])
                    
                history_data["labels"].append(dt_obj.strftime("%H:%M:%S"))
                history_data["temperature"].append(row['temperature'])
                history_data["humidity"].append(row['humidity'])
                history_data["soil_moisture"].append(row['soil_moisture'])

    finally:
        conn.close()

    return {
        "latest": latest_reading_overall,
        "nodes": all_nodes,
        "history": history_data
    }

# -----------------------
# WebSocket Server (Para ESP32)
# -----------------------
WS_CLIENTS = set()

async def ws_handler(websocket):
    """Maneja conexiones WebSocket desde ESP32"""
    WS_CLIENTS.add(websocket)
    client_ip = websocket.remote_address[0]
    print(f"🔌 WS cliente conectado: {client_ip}")
    
    try:
        async for message in websocket:
            print(f"📥 WS Recibido de ESP32: {message}")
            
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                print("⚠️ WS: mensaje no es JSON válido")
                try: 
                    await websocket.send(json.dumps({"ok": False, "error": "Invalid JSON"}))
                except: 
                    pass
                continue

            if isinstance(data, dict):
                # Corregir si el ESP32 envía 'lon' en lugar de 'lng'
                if "lon" in data and "lng" not in data:
                    data["lng"] = data["lon"]
            else:
                print("⚠️ WS: mensaje no es un objeto JSON")
                continue

            try:
                handle_new_reading(data)
                await websocket.send(json.dumps({"ok": True}))
            except Exception as e:
                print(f"❌ Error procesando lectura: {e}")
                try:
                    await websocket.send(json.dumps({"ok": False, "error": str(e)}))
                except: 
                    pass
                    
    except websockets.ConnectionClosed:
        print(f"🔌 WS cliente desconectado: {client_ip}")
    except Exception as e:
        print(f"❌ WS handler error: {e}")
    finally:
        WS_CLIENTS.discard(websocket)

def start_ws_server_loop():
    """Inicia el servidor WebSocket en un hilo separado"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def runner():
        async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT):
            print(f"🌐 WebSocket server escuchando en ws://0.0.0.0:{WS_PORT}")
            await asyncio.Future()  # Ejecutar para siempre

    try:
        loop.run_until_complete(runner())
    except Exception as e:
        print(f"❌ Error en WebSocket server: {e}")

# -----------------------
# MAIN
# -----------------------
if __name__ == "__main__":
    print("=" * 60)
    print("🚀 Iniciando Red de Sensores Ad Hoc - Backend")
    print("=" * 60)
    
    # Inicializar base de datos
    try:
        init_db()
    except Exception as e:
        print(f"❌ Error inicializando BD: {e}")
        exit(1)

    # Iniciar servidor WebSocket en hilo separado
    ws_thread = threading.Thread(target=start_ws_server_loop, daemon=True)
    ws_thread.start()

    # Iniciar Flask + Socket.IO
    print(f"🌐 Iniciando Flask-SocketIO en puerto {PORT}...")
    print(f"📊 Base de datos: {'PostgreSQL (Supabase)' if USE_POSTGRES else 'SQLite (local)'}")
    print("=" * 60)
    
    socketio.run(app, debug=False, port=PORT, host='0.0.0.0')
