import requests
import base64
import json
import re
import yfinance as yf
import time
import logging
import warnings
import concurrent.futures
import os
from functools import lru_cache
from difflib import SequenceMatcher  # <--- NUEVO: Para comparar nombres
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==========================================
# CONFIGURACIÓN DE LA API Y LOGS
# ==========================================

# 1. Suprimir warnings y logs ruidosos de yfinance/requests
warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("requests").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)

app = FastAPI(title="Trading212 & Yahoo Finance API")

# Configurar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==========================================
# OPTIMIZACIÓN DE CONEXIONES (NUEVO)
# ==========================================
# Creamos una sesión global para reutilizar conexiones TCP
session = requests.Session()
retry_strategy = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
)
# Pool grande para soportar los hilos paralelos
adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)
session.headers.update({'User-Agent': 'Mozilla/5.0'})


# 2. Modelo de datos para la entrada (Input) - INTACTO
class TradingCredentials(BaseModel):
    api_key: str
    api_secret: str

# ==========================================
# UTILIDADES DE TEXTO (NUEVO)
# ==========================================
def son_nombres_similares(nombre1, nombre2):
    """Devuelve True si los nombres se parecen razonablemente."""
    if not nombre1 or not nombre2:
        return False
    
    # Limpieza básica
    n1 = re.sub(r'\W+', ' ', nombre1.lower()).strip()
    n2 = re.sub(r'\W+', ' ', nombre2.lower()).strip()
    
    # 1. Chequeo directo de contención
    if n1 in n2 or n2 in n1:
        return True
        
    # 2. Similitud difusa (> 50%)
    ratio = SequenceMatcher(None, n1, n2).ratio()
    return ratio > 0.5

# ==========================================
# LÓGICA DE NEGOCIO (OPTIMIZADA)
# ==========================================

def get_auth_header(api_key, api_secret):
    credentials = f"{api_key}:{api_secret}"
    encoded = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
    return {"Authorization": f"Basic {encoded}"}

def rescatar_ticker_por_isin(isin):
    url = f"https://query2.finance.yahoo.com/v1/finance/search?q={isin}"
    try:
        # Usamos la sesión global optimizada
        r = session.get(url, timeout=5)
        data = r.json()
        if 'quotes' in data and len(data['quotes']) > 0:
            candidatos = [q for q in data['quotes'] if q.get('quoteType') == 'EQUITY']
            if candidatos:
                return candidatos[0]['symbol']
            else:
                return data['quotes'][0]['symbol']
    except Exception:
        pass
    return None

def obtener_datos_yahoo(ticker_yahoo, isin_respaldo=None):
    datos_vacios = {"strong_buy": 0, "buy": 0, "neutral": 0, "sell": 0, "strong_sell": 0}
    
    if not ticker_yahoo and not isin_respaldo: 
        return datos_vacios, None

    ticker_final = ticker_yahoo
    
    try:
        # yfinance instancia sus propias conexiones, pero gestionamos el rescate con nuestra session
        stock = yf.Ticker(ticker_final)
        recs = stock.recommendations
        
        # Plan B: Buscar por ISIN si el ticker falla
        if (recs is None or recs.empty) and isin_respaldo:
            nuevo_ticker = rescatar_ticker_por_isin(isin_respaldo)
            if nuevo_ticker and nuevo_ticker != ticker_final:
                ticker_final = nuevo_ticker
                stock = yf.Ticker(nuevo_ticker)
                recs = stock.recommendations

        if recs is None or recs.empty:
            return datos_vacios, ticker_final

        fila = recs.iloc[0]
        datos = {
            "strong_buy": int(fila.get('strongBuy', 0)),
            "buy": int(fila.get('buy', 0)),
            "neutral": int(fila.get('hold', 0)), 
            "sell": int(fila.get('sell', 0)),
            "strong_sell": int(fila.get('strongSell', 0))
        }
        return datos, ticker_final

    except Exception:
        # Último intento desesperado
        if isin_respaldo and ticker_final == ticker_yahoo:
            try:
                nuevo_ticker = rescatar_ticker_por_isin(isin_respaldo)
                if nuevo_ticker:
                    stock = yf.Ticker(nuevo_ticker)
                    recs = stock.recommendations
                    if recs is not None and not recs.empty:
                        fila = recs.iloc[0]
                        datos = {
                            "strong_buy": int(fila.get('strongBuy', 0)),
                            "buy": int(fila.get('buy', 0)),
                            "neutral": int(fila.get('hold', 0)), 
                            "sell": int(fila.get('sell', 0)),
                            "strong_sell": int(fila.get('strongSell', 0))
                        }
                        return datos, nuevo_ticker
            except:
                pass
                
        return datos_vacios, ticker_final

def calcular_consenso_ponderado(sb, b, n, s, ss):
    total = sb + b + n + s + ss
    if total == 0: return "N/A"
    promedio = ((sb*5) + (b*4) + (n*3) + (s*2) + (ss*1)) / total
    
    if promedio >= 4.5: return "STRONG BUY"
    elif promedio >= 3.5: return "BUY"
    elif promedio >= 2.5: return "NEUTRAL"
    elif promedio >= 1.5: return "SELL"
    else: return "STRONG SELL"

# Cacheamos para no repetir peticiones si el usuario refresca o hay acciones repetidas
@lru_cache(maxsize=256)
def buscar_mercado_principal_tv(isin_origen):
    """
    Retorna:
    (Symbol Main, Country Main, Exchange Main, ISIN Main, Name Main, Symbol Original, Country Original)
    """
    url = "https://symbol-search.tradingview.com/symbol_search/v3/"
    headers_tv = {"Origin": "https://www.tradingview.com"} # Session ya tiene User-Agent
    try:
        resp = session.get(url, params={"text": isin_origen, "hl": "1", "domain": "production"}, headers=headers_tv)
        data = resp.json()
        if not data.get('symbols'): return None, None, None, None, None, None, None
        
        activo_inicial = data['symbols'][0]
        activo_final = activo_inicial
        
        # Búsqueda de mercado padre/principal
        if activo_final.get('type') in ['dr', 'fund', 'structured']:
            nombre_limpio = re.split(r'(?i)(\s(inc\.|ltd\.|plc|corp\.|gdr|adr|sponsored))', activo_final['description'])[0].strip()
            resp_nom = session.get(url, params={"text": nombre_limpio, "domain": "production"}, headers=headers_tv)
            data_nom = resp_nom.json()
            candidatos = [x for x in data_nom.get('symbols', []) if x.get('type') == 'stock' and x.get('isin') != isin_origen]
            if candidatos: 
                activo_final = candidatos[0]

        return (
            activo_final['symbol'], 
            activo_final['country'], 
            activo_final['exchange'], 
            activo_final.get('isin'), 
            activo_final.get('description'), # Nombre Main
            activo_inicial['symbol'],        # Ticker original
            activo_inicial['country']        # País original
        )
    except:
        return None, None, None, None, None, None, None

def convertir_a_ticker_yahoo(symbol, country):
    if not symbol: return None
    sufijos = {
        "KR": ".KS", "DE": ".DE", "ES": ".MC", "FR": ".PA", "GB": ".L",
        "HK": ".HK", "JP": ".T", "TW": ".TW", "CN": ".SS", "IN": ".NS",
        "CA": ".TO", "AU": ".AX", "IT": ".MI", "NL": ".AS", "BR": ".SA", "US": ""
    }
    if country == "HK": symbol = symbol.zfill(4)
    return f"{symbol}{sufijos.get(country, '')}"

def detect_environment(api_key, api_secret):
    envs = ['live', 'demo']
    headers = get_auth_header(api_key, api_secret)
    for env in envs:
        try:
            r = session.get(f"https://{env}.trading212.com/api/v0/equity/metadata/exchanges", headers=headers, timeout=5)
            if r.status_code == 200: return f"https://{env}.trading212.com"
        except: pass
    return None

# ==========================================
# WORKER PARA PROCESAMIENTO PARALELO
# ==========================================
def process_single_position(p, total_value):
    """Procesa una única posición. Diseñado para correr en un hilo."""
    try:
        inst = p.get('instrument', {})
        ticker_raw = inst.get('ticker', 'UNKNOWN')
        isin_t212 = inst.get('isin', 'N/A')
        name_t212 = inst.get('name', 'Unknown') # Obtenemos nombre de T212
        ticker_clean = ticker_raw.split('_')[0]

        # 1. TV Intelligence (Obtenemos también nombres y originales)
        sym, pais, exch, isin_main, name_tv, sym_orig, pais_orig = buscar_mercado_principal_tv(isin_t212)
        
        # LOGICA DE VERIFICACIÓN DE NOMBRE
        match_nombre = False
        if name_tv:
            match_nombre = son_nombres_similares(name_t212, name_tv)
            
        # Si el nombre coincide, usamos el mercado principal.
        # Si NO coincide, usamos el ISIN de T212 y el ticker que TV da para ese ISIN.
        if match_nombre:
            isin_final_para_json = isin_main if isin_main else isin_t212
            ticker_yahoo_calc = convertir_a_ticker_yahoo(sym, pais)
        else:
            isin_final_para_json = isin_t212 # Revertimos al de T212
            ticker_yahoo_calc = convertir_a_ticker_yahoo(sym_orig, pais_orig)

        # 2. Yahoo Data
        # Usamos isin_final_para_json como respaldo en Yahoo
        r, ticker_real_usado = obtener_datos_yahoo(ticker_yahoo_calc, isin_respaldo=isin_final_para_json)
        
        # 3. Rating
        analyst_rating_calc = calcular_consenso_ponderado(
            r['strong_buy'], r['buy'], r['neutral'], r['sell'], r['strong_sell']
        )
        
        current_val = p.get('walletImpact', {}).get('currentValue', 0)
        
        pos_data = {
            "ticker": ticker_clean,
            "full_ticker": ticker_raw,
            "isin_origen": isin_t212,
            "name": name_t212,
            "quantity": p.get('quantity', 0),
            "current_value": round(current_val, 2),
            "portfolio_percentage": round((current_val / total_value * 100), 2) if total_value else 0,
            
            "isin_principal": isin_final_para_json,
            "ticker_Mercado_principal": ticker_real_usado if ticker_real_usado else "N/A",
            "analyst_rating": analyst_rating_calc,
            "recommendations": r
        }
        return pos_data
    except Exception:
        # Si falla, retornamos None para filtrarlo después y no romper el flujo
        return None

def process_portfolio(base_url, api_key, api_secret):
    headers = get_auth_header(api_key, api_secret)
    url = f"{base_url}/api/v0/equity/positions"

    # Usamos session para T212 también
    response = session.get(url, headers=headers)
    
    if response.status_code == 401:
        raise ValueError("Credenciales de Trading212 inválidas.")
    if response.status_code != 200:
        raise Exception(f"Error de API Trading212: {response.status_code}")

    positions = response.json()
    if not isinstance(positions, list):
         return {"summary": {"total_portfolio_value": 0, "positions_count": 0}, "positions": []}

    total_value = sum(p.get('walletImpact', {}).get('currentValue', 0) for p in positions)
    portfolio_data = []

    # ==========================================
    # EJECUCIÓN PARALELA (ThreadPoolExecutor)
    # ==========================================
    # Lanzamos 20 hilos simultáneos
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        # Preparamos las tareas
        futures = [executor.submit(process_single_position, p, total_value) for p in positions]
        
        # Recogemos resultados conforme terminan
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                portfolio_data.append(result)

    # Ordenamos el resultado final
    portfolio_data.sort(key=lambda x: x['portfolio_percentage'], reverse=True)

    return {
        "summary": {"total_portfolio_value": round(total_value, 2), "positions_count": len(positions)},
        "positions": portfolio_data
    }

# ==========================================
# ENDPOINT DE LA API
# ==========================================

@app.post("/analyze-portfolio")
async def analyze_portfolio(creds: TradingCredentials):
    """
    Recibe API Key y Secret de Trading212.
    Devuelve JSON con análisis enriquecido de Yahoo Finance.
    """
    try:
        # 1. Detectar entorno (Live vs Demo)
        base_url = detect_environment(creds.api_key, creds.api_secret)
        
        if not base_url:
            return {"status": "error", "message": "No se pudo autenticar. Verifique API Key y Secret."}

        # 2. Procesar datos
        data = process_portfolio(base_url, creds.api_key, creds.api_secret)
        
        return {"status": "success", "data": data}

    except ValueError as ve:
        return {"status": "error", "message": str(ve)}
        
    except Exception as e:
        return {"status": "error", "message": f"Error interno del servidor: {str(e)}"}

# Montar archivos estáticos
static_dir = os.path.join(os.path.dirname(__file__), "static")
if not os.path.exists(static_dir):
    os.makedirs(static_dir)

app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    # Ajustar threads del servidor uvicorn también si es necesario
    uvicorn.run(app, host="0.0.0.0", port=8000)