from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO
import sqlite3
import psycopg2
from psycopg2.extras import RealDictCursor
from pathlib import Path
from datetime import datetime
import json
import os

# -----------------------
# CONFIGURACIÓN
# -----------------------
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
# Guardar lectura de UN nodo
# -----------------------
def save_single_node_reading(data):
    """Procesa y guarda una lectura de UN SOLO nodo"""
    node_name = str(data.get("nodeID"))
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
                data.get("soil_moisture", 0.0),
                data.get("temperature", 0.0),
                data.get("humidity", 0.0),
                data.get("lux", 0.0)
            ))
        else:
            cur.execute("""
                INSERT INTO readings (node_id, timestamp, soil_moisture, temperature, humidity, lux)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                node_id,
                datetime.now().isoformat(),
                data.get("soil_moisture", 0.0),
                data.get("temperature", 0.0),
                data.get("humidity", 0.0),
                data.get("lux", 0.0)
            ))

        conn.commit()
        print(f"✅ Lectura guardada - Nodo: {node_name}")
        return True
        
    except Exception as e:
        print(f"❌ Error guardando lectura del nodo {node_name}: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()

# -----------------------
# Procesar datos consolidados de múltiples nodos
# -----------------------
def handle_consolidated_data(data):
    """
    Procesa datos consolidados del root node.
    Formato esperado:
    {
        "nodes": [
            {"nodeID": 123, "temperature": 21.5, ...},
            {"nodeID": 456, "humidity": 60.0, ...}
        ],
        "timestamp": 123456,
        "totalNodes": 2,
        "activeNodes": 2,
        "rootNodeID": 789
    }
    """
    nodes = data.get("nodes", [])
    
    if not nodes:
        print("⚠️ No hay nodos en los datos consolidados")
        return 0
    
    saved_count = 0
    errors = []
    
    print(f"📦 Procesando {len(nodes)} nodos del paquete consolidado")
    
    for node_data in nodes:
        try:
            save_single_node_reading(node_data)
            saved_count += 1
        except Exception as e:
            node_id = node_data.get("nodeID", "unknown")
            error_msg = f"Nodo {node_id}: {str(e)}"
            errors.append(error_msg)
            print(f"❌ {error_msg}")
    
    print(f"✅ Guardados {saved_count}/{len(nodes)} nodos")
    
    if errors:
        print(f"⚠️ Errores: {len(errors)}")
        for error in errors:
            print(f"   - {error}")
    
    # Emitir actualización a Socket.IO para el front-end
    try:
        update_data = fetch_latest_data()
        socketio.emit('new_reading', update_data)
        print("📡 Datos emitidos a clientes Socket.IO")
    except Exception as e:
        print(f"⚠️ Error emitiendo datos a Socket.IO: {e}")
    
    return saved_count

# -----------------------
# Flask Routes (HTTP)
# -----------------------
@app.route('/')
def index():
    """Renderiza el dashboard principal"""
    return render_template('index.html')

@app.route('/api/latest', methods=['GET'])
def get_latest():
    """Endpoint para obtener la última lectura de cada nodo"""
    try:
        data = fetch_latest_data()
        return jsonify(data.get('all_latest', []))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/history', methods=['GET'])
def get_history():
    """Endpoint para obtener históricos"""
    try:
        node_id = request.args.get('node_id')
        data = fetch_history_data(node_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/nodes', methods=['GET'])
def get_nodes():
    """Endpoint para obtener información de todos los nodos"""
    try:
        data = fetch_latest_data()
        return jsonify(data.get('nodes', []))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/data', methods=['GET'])
def get_data():
    """Endpoint completo con todos los datos de todos los nodos"""
    try:
        data = fetch_latest_data()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# -----------------------
# 🆕 ENDPOINT PRINCIPAL PARA ESP32 (Datos Consolidados)
# -----------------------
@app.route('/api/sensor-data', methods=['POST'])
def receive_sensor_data():
    """
    Endpoint HTTP POST para recibir datos del ESP32 Root Node
    Soporta AMBOS formatos:
    1. Datos consolidados: {"nodes": [...], "timestamp": ..., ...}
    2. Datos individuales: {"nodeID": 123, "temperature": 21.5, ...}
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"ok": False, "error": "No JSON data received"}), 400
        
        # Detectar formato de datos
        if "nodes" in data:
            # ✅ FORMATO CONSOLIDADO (múltiples nodos)
            print(f"📦 Datos CONSOLIDADOS recibidos del ESP32")
            print(f"   Total nodos: {data.get('totalNodes', 0)}")
            print(f"   Activos: {data.get('activeNodes', 0)}")
            print(f"   Root NodeID: {data.get('rootNodeID', 'N/A')}")
            
            saved_count = handle_consolidated_data(data)
            
            return jsonify({
                "ok": True, 
                "message": f"Consolidated data saved successfully",
                "nodes_processed": len(data.get('nodes', [])),
                "nodes_saved": saved_count
            }), 200
            
        else:
            # ✅ FORMATO INDIVIDUAL (un solo nodo) - Retrocompatibilidad
            print(f"📥 Datos INDIVIDUALES recibidos del ESP32: {data.get('nodeID', 'unknown')}")
            
            save_single_node_reading(data)
            
            # Emitir actualización
            update_data = fetch_latest_data()
            socketio.emit('new_reading', update_data)
            
            return jsonify({
                "ok": True, 
                "message": "Single node data saved successfully"
            }), 200
        
    except Exception as e:
        print(f"❌ Error procesando datos del ESP32: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check para Render y monitoreo"""
    return jsonify({
        "status": "healthy", 
        "timestamp": datetime.now().isoformat(),
        "database": "PostgreSQL" if USE_POSTGRES else "SQLite",
        "port": PORT,
        "features": ["consolidated_data", "multi_node_support"]
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
    """Obtiene los datos más recientes de TODOS los nodos"""
    conn = get_db()
    cur = conn.cursor()

    try:
        # Obtener última lectura de cada nodo
        cur.execute("""
            SELECT 
                r.*, 
                n.node_name, 
                n.lat, 
                n.lng,
                n.online
            FROM readings r 
            JOIN nodes n ON r.node_id = n.id 
            WHERE r.id IN (
                SELECT MAX(id) FROM readings GROUP BY node_id
            ) 
            ORDER BY r.timestamp DESC;
        """)
        all_latest_readings = [dict(row) for row in cur.fetchall()]

        # Obtener info de todos los nodos
        cur.execute("SELECT node_name, lat, lng, online, last_seen FROM nodes;")
        all_nodes = [dict(row) for row in cur.fetchall()]
        
        # Para retrocompatibilidad, mantener "latest" con el nodo más reciente
        latest_reading_overall = all_latest_readings[0] if all_latest_readings else {
            "timestamp": None,
            "soil_moisture": None,
            "temperature": None,
            "humidity": None,
            "lux": None
        }

    finally:
        conn.close()

    return {
        "latest": latest_reading_overall,  # El más reciente (retrocompatibilidad)
        "all_latest": all_latest_readings,  # TODAS las últimas lecturas de cada nodo
        "nodes": all_nodes,
        "total_nodes": len(all_nodes),
        "active_nodes": sum(1 for n in all_nodes if n.get('online', 0) == 1)
    }

def fetch_history_data(node_id=None):
    """Obtiene histórico de un nodo específico o del más reciente"""
    conn = get_db()
    cur = conn.cursor()

    try:
        history_data = {"labels": [], "temperature": [], "humidity": [], "soil_moisture": [], "lux": []}
        
        if node_id:
            target_node_id = int(node_id)
        else:
            # Si no se especifica, usar el nodo más reciente
            cur.execute("""
                SELECT node_id FROM readings 
                ORDER BY timestamp DESC LIMIT 1
            """)
            result = cur.fetchone()
            if not result:
                return history_data
            target_node_id = result['node_id']
        
        if USE_POSTGRES:
            cur.execute("""
                SELECT timestamp, temperature, humidity, soil_moisture, lux
                FROM readings 
                WHERE node_id = %s 
                ORDER BY timestamp DESC 
                LIMIT 50;
            """, (target_node_id,))
        else:
            cur.execute("""
                SELECT timestamp, temperature, humidity, soil_moisture, lux
                FROM readings 
                WHERE node_id = ? 
                ORDER BY timestamp DESC 
                LIMIT 50;
            """, (target_node_id,))
        
        history = cur.fetchall()
        history = list(reversed(history))  # Invertir para orden cronológico

        for row in history:
            if USE_POSTGRES:
                dt_obj = row['timestamp']
            else:
                dt_obj = datetime.fromisoformat(row['timestamp'])
                
            history_data["labels"].append(dt_obj.strftime("%H:%M:%S"))
            history_data["temperature"].append(row['temperature'])
            history_data["humidity"].append(row['humidity'])
            history_data["soil_moisture"].append(row['soil_moisture'])
            history_data["lux"].append(row.get('lux', 0.0))

    finally:
        conn.close()

    return history_data

# -----------------------
# MAIN
# -----------------------
if __name__ == "__main__":
    print("=" * 60)
    print("🚀 Iniciando Red de Sensores Ad Hoc - Backend")
    print("   📦 SOPORTE PARA DATOS CONSOLIDADOS ACTIVADO")
    print("=" * 60)
    
    # Inicializar base de datos
    try:
        init_db()
    except Exception as e:
        print(f"❌ Error inicializando BD: {e}")
        exit(1)

    # Iniciar Flask + Socket.IO
    print(f"🌐 Iniciando Flask-SocketIO en puerto {PORT}...")
    print(f"📊 Base de datos: {'PostgreSQL (Render)' if USE_POSTGRES else 'SQLite (local)'}")
    print(f"✅ Formatos soportados:")
    print(f"   • Datos consolidados (múltiples nodos)")
    print(f"   • Datos individuales (retrocompatibilidad)")
    print("=" * 60)
    
    socketio.run(app, debug=False, port=PORT, host='0.0.0.0')
