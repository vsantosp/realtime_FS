import os
import json
import pandas as pd
import numpy as np
import xml.etree.ElementTree as ET
from sqlalchemy import create_engine
from dateutil.parser import parse
from dateutil import tz
from collections import defaultdict
import time
from datetime import datetime

# ====================== CONFIGURACIÓN ===========================
db_config = {
    'user': 'postgres',
    'password': 'bettercare',
    'host': 'localhost',
    'port': 5432,
    'database': 'VeroClassifier'
}

basedir = r"C:\Users\vsantos\Documents\REALTIME_FS"
patientsprocessed_folder = os.path.join(basedir, "Patients")
output_json_folder = os.path.join(basedir, "SimulatedRealtimeJSON")

if not os.path.exists(output_json_folder):
    os.makedirs(output_json_folder)

# ================== CONEXIÓN A LA BASE DE DATOS =================
engine = create_engine(f"postgresql://{db_config['user']}:{db_config['password']}@{db_config['host']}:{db_config['port']}/{db_config['database']}")

# ==================== FUNCIONES XML =============================
def add_padding(s):
    padding_length = 4 - (len(s) % 4)
    return s + '=' * padding_length

def encode_data(data):
    data_bytes = data.encode('cp850')
    return list(data_bytes)

def extract_signal_from_xml(file, signal_type):
    tree = ET.parse(file)
    root = tree.getroot()
    signal_index = None
    for i, nw_element in enumerate(root.findall(".//NW")):
        wn_element = nw_element.find("WN")
        if wn_element is not None and wn_element.text == signal_type:
            signal_index = i
            break
    if signal_index is None:
        raise ValueError(f"No se encontró la curva '{signal_type}' en {file}")
    curve_data = root.findall(".//NW/WS")[signal_index].text
    curve_data_padded = add_padding(curve_data)
    decoded_data = encode_data(curve_data_padded)
    values_length = len(decoded_data)
    ret = []
    for i in range(0, values_length, 2):
        byte1 = decoded_data[i]
        byte2 = decoded_data[i + 1]
        ret.append(((byte1 - 63) / 100) + ((byte2 - 63) / 10000))
    mi = float(root.findall(".//NW/MI")[signal_index].text)
    ma = float(root.findall(".//NW/MA")[signal_index].text)
    signal = [mi + (ma - mi) * val for val in ret]
    return signal[:-2]

def get_date_from_xml(file, tag):
    tree = ET.parse(file)
    root = tree.getroot()
    date = root.find(f".//{tag}").text
    return pd.to_datetime(date.replace("T", " "), utc=True)

def load_patient_signals(nhc):
    """Cargar todas las señales de un paciente de una vez"""
    print(f"    Cargando señales para paciente {nhc}...")
    
    nhc_folder = os.path.join(patientsprocessed_folder, nhc)
    if not os.path.exists(nhc_folder):
        print(f"    No existe carpeta para NHC: {nhc}")
        return None
    
    patient_data = {}
    
    try:
        for bed in os.listdir(nhc_folder):
            bed_path = os.path.join(nhc_folder, bed)
            if not os.path.isdir(bed_path):
                continue
                
            for date_folder in os.listdir(bed_path):
                full_path = os.path.join(bed_path, date_folder)
                if not os.path.isdir(full_path):
                    continue
                    
                files = sorted([f for f in os.listdir(full_path) if f.startswith("message") and f.endswith(".xml")])
                if not files:
                    continue
                    
                try:
                    ini_date = get_date_from_xml(os.path.join(full_path, files[0]), "TI")
                    end_date = get_date_from_xml(os.path.join(full_path, files[-1]), "TE")
                    
                    print(f"      Cargando desde {bed}/{date_folder} ({len(files)} archivos)")
                    
                    # Extraer señales completas
                    original_dir = os.getcwd()
                    os.chdir(full_path)
                    
                    paw = []
                    flow = []
                    for f in files:
                        try:
                            paw += extract_signal_from_xml(f, "PAW")
                            flow += extract_signal_from_xml(f, "AIR FLOW")
                        except Exception as e:
                            print(f"      Error extrayendo señales de {f}: {e}")
                            continue
                    
                    os.chdir(original_dir)
                    
                    if paw and flow:
                        patient_data[f"{bed}_{date_folder}"] = {
                            'paw': paw,
                            'flow': flow,
                            'ini_date': ini_date,
                            'end_date': end_date
                        }
                        print(f"      Señales cargadas: {len(paw)} muestras")
                    
                except Exception as e:
                    print(f"      Error procesando {date_folder}: {e}")
                    continue
    
    except Exception as e:
        print(f"    Error procesando NHC {nhc}: {e}")
        return None
    
    return patient_data if patient_data else None

# ==================== FUNCIÓN PRINCIPAL =========================
def simulate_realtime_json():
    print("="*60)
    print("INICIANDO SIMULACIÓN DE TIEMPO REAL")
    print("="*60)
    
    print("Cargando datos de la base de datos...")
    df = pd.read_sql('SELECT * FROM public.breathdata', engine, parse_dates=["Time"])
    print(f"Datos cargados: {len(df)} registros")
    
    # Mostrar información sobre pacientes únicos
    unique_patients = df['NHC'].nunique()
    print(f"Pacientes únicos: {unique_patients}")
    print(f"NHCs: {df['NHC'].unique()}")
    
    df = df.sort_values(by=["NHC", "Time", "Breath Order"]).reset_index(drop=True)

    # Calcular ti_new CON TODOS LOS DATOS
    df["next_time"] = df["Time"].shift(-1)
    df["next_nhc"] = df["NHC"].shift(-1)
    df["next_center"] = df["center"].shift(-1)
    
    # Asegurar que el siguiente ciclo es del mismo paciente y centro
    same_patient = (df["NHC"] == df["next_nhc"]) & (df["center"] == df["next_center"])
    
    # Calcular ti_new
    df["t_aux"] = df["next_time"] - pd.to_timedelta(df["Expiratory_Time"], unit='s')
    df["ti_new"] = (df["t_aux"] - df["Time"]).dt.total_seconds()

    # Filtrar solo registros válidos (con ti_new calculado) y ti_new positivo
    df = df[same_patient & df["ti_new"].notnull() & (df["ti_new"] > 0)]
    
    print(f"Datos después del filtro: {len(df)} registros")
    print(f"Pacientes después del filtro: {df['NHC'].nunique()}")
    
    if len(df) == 0:
        print("No hay datos válidos para procesar")
        return

    # Calcular minutos POR PACIENTE desde su primer timestamp
    df["minute"] = df.groupby("NHC")["Time"].transform(lambda x: ((x - x.min()).dt.total_seconds() / 60).astype(int))
    
    # Mostrar información sobre minutos
    total_minutes = df["minute"].max() + 1
    print(f"Total de minutos a procesar: {int(total_minutes)}")
    print(f"Minutos únicos: {sorted(df['minute'].unique())}")
    
    # Verificar distribución de datos por minuto y paciente
    minute_counts = df['minute'].value_counts().sort_index()
    print("Distribución de registros por minuto:")
    for minute, count in minute_counts.items():
        print(f"  Minuto {minute}: {count} registros")
    
    # Mostrar distribución por paciente
    print("\nDistribución por paciente y minuto:")
    for nhc in df['NHC'].unique():
        patient_data = df[df['NHC'] == nhc]
        patient_minutes = patient_data['minute'].value_counts().sort_index()
        print(f"  Paciente {nhc}: minutos {list(patient_minutes.index)} con {list(patient_minutes.values)} registros")
    
    freq = 1 / 0.005  # 200 Hz
    
    # OPTIMIZACIÓN: Cargar señales por paciente una sola vez
    patient_signals = {}
    unique_nhcs = df['NHC'].unique()
    
    print(f"\nCargando señales para {len(unique_nhcs)} pacientes...")
    for nhc in unique_nhcs:
        patient_signals[nhc] = load_patient_signals(nhc)
    
    print(f"Señales cargadas para {len([k for k, v in patient_signals.items() if v is not None])} pacientes")
    
    # ==================== SIMULACIÓN TIEMPO REAL ====================
    print("\n" + "="*60)
    print("COMENZANDO SIMULACIÓN EN TIEMPO REAL")
    print("Generando un archivo JSON cada 60 segundos...")
    print("="*60)
    
    # Procesar por minutos CON ESPERA
    grouped_data = df.groupby("minute")
    total_minutes_to_process = len(grouped_data)
    
    for minute_idx, (minute, group) in enumerate(grouped_data, 1):
        start_time = datetime.now()
        
        print(f"\n[{start_time.strftime('%H:%M:%S')}] Procesando minuto {int(minute)} ({minute_idx}/{total_minutes_to_process})")
        print(f"  Registros en este minuto: {len(group)}")
        print(f"  Pacientes únicos: {group['NHC'].nunique()}")
        print(f"  NHCs: {group['NHC'].unique()}")
        
        records = []
        
        for idx, row in group.iterrows():
            nhc = row["NHC"]
            cycle_time = row["Time"]
            ti = row["ti_new"]
            te = row["Expiratory_Time"]
            mode = row.get("BCMode_20", "Unknown")
            trigger = row.get("Trigger", "Unknown")
            
            print(f"    Procesando NHC: {nhc}, Tiempo: {cycle_time}, TI: {ti:.3f}s")

            # Verificar si tenemos señales cargadas para este paciente
            if nhc not in patient_signals or patient_signals[nhc] is None:
                print(f"      No hay señales disponibles para NHC: {nhc}")
                continue
            
            # Asegurar que cycle_time tenga timezone
            cycle_time = cycle_time.tz_localize('UTC') if cycle_time.tzinfo is None else cycle_time
            
            # Buscar el rango de datos correcto
            found = False
            for data_key, data in patient_signals[nhc].items():
                ini_date = data['ini_date']
                end_date = data['end_date']
                
                if ini_date <= cycle_time <= end_date:
                    print(f"      Usando datos de {data_key}")
                    
                    paw = data['paw']
                    flow = data['flow']
                    
                    # Calcular índice de inicio
                    ini_idx = int(round((cycle_time - ini_date).total_seconds() * freq))
                    ti_samples = int(round(ti * freq))
                    
                    # Verificar que los índices sean válidos
                    if ini_idx < 0:
                        print(f"      Índice de inicio negativo: {ini_idx}, ajustando a 0")
                        ini_idx = 0
                    
                    if ini_idx >= len(paw):
                        print(f"      Índice de inicio fuera de rango: {ini_idx} >= {len(paw)}")
                        continue
                        
                    end_idx = min(ini_idx + ti_samples, len(paw))
                    paw_segment = paw[ini_idx:end_idx]
                    flow_segment = flow[ini_idx:end_idx] if end_idx <= len(flow) else flow[ini_idx:len(flow)]
                    
                    if not paw_segment or not flow_segment:
                        print(f"      Segmentos vacíos (paw: {len(paw_segment)}, flow: {len(flow_segment)})")
                        continue
                    
                    records.append({
                        "NHC": nhc,
                        "Time": str(cycle_time),
                        "Inspiratory_Time": round(ti, 3),
                        "Expiratory_Time": round(te, 3),
                        "ti_new": round(ti, 3),
                        "BCMode_20": mode,
                        "Trigger": trigger,
                        "Pressure": paw_segment,
                        "Flow": flow_segment
                    })
                    
                    print(f"      Registro agregado exitosamente (samples: {len(paw_segment)})")
                    found = True
                    break
            
            if not found:
                print(f"      No se encontró rango de tiempo válido para {cycle_time}")
        
        # Generar archivo JSON
        if records:
            output_file = os.path.join(output_json_folder, f"minute_{int(minute):03}.json")
            with open(output_file, "w") as f:
                json.dump(records, f, indent=2)
            
            processing_time = (datetime.now() - start_time).total_seconds()
            print(f"  ✓ Archivo generado: {output_file} con {len(records)} registros")
            print(f"  ✓ Tiempo de procesamiento: {processing_time:.2f} segundos")
        else:
            print(f"  ✗ No se generaron registros para el minuto {int(minute)}")
        
        # ESPERAR 60 SEGUNDOS ANTES DEL SIGUIENTE MINUTO
        if minute_idx < total_minutes_to_process:  # No esperar después del último minuto
            print(f"  ⏳ Esperando 60 segundos antes del siguiente minuto...")
            print(f"  ⏳ Progreso: {minute_idx}/{total_minutes_to_process} minutos completados")
            
            # Mostrar cuenta regresiva cada 10 segundos
            for remaining in range(60, 0, -10):
                time.sleep(10)
                if remaining > 10:
                    print(f"    ⏱️  {remaining-10} segundos restantes...")
            
            print(f"  ✅ Continuando con el siguiente minuto...")
    
    print("\n" + "="*60)
    print("SIMULACIÓN COMPLETADA")
    print("="*60)
    print(f"Total de archivos JSON generados: {len([f for f in os.listdir(output_json_folder) if f.endswith('.json')])}")
    print(f"Tiempo total de simulación: {total_minutes_to_process} minutos")
    print("="*60)

if __name__ == "__main__":
    simulate_realtime_json()