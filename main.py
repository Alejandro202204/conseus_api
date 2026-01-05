import requests
import base64
import json
import re
import yfinance as yf
import time
import logging
import warnings
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any

# ==========================================
# CONFIGURACIÓN DE LA API Y LOGS
# ==========================================

# 1. Suprimir warnings y logs ruidosos de yfinance/requests
warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("requests").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)

app = FastAPI(title="Trading212 & Yahoo Finance API")

# 2. Modelo de datos para la entrada (Input)
class TradingCredentials(BaseModel):
    api_key: str
    api_secret: str

# ==========================================
# LÓGICA DE NEGOCIO (ADAPTADA)
# ==========================================

def get_auth_header(api_key, api_secret):
    credentials = f"{api_key}:{api_secret}"
    encoded = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
    return {"Authorization": f"Basic {encoded}"}

def rescatar_ticker_por_isin(isin):
    url = f"https://query2.finance.yahoo.com/v1/finance/search?q={isin}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        r = requests.get(url, headers=headers, timeout=5)
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

def buscar_mercado_principal_tv(isin_origen):
    url = "https://symbol-search.tradingview.com/symbol_search/v3/"
    headers = {"User-Agent": "Mozilla/5.0", "Origin": "https://www.tradingview.com"}
    try:
        resp = requests.get(url, params={"text": isin_origen, "hl": "1", "domain": "production"}, headers=headers)
        data = resp.json()
        if not data.get('symbols'): return None, None, None, None
        activo = data['symbols'][0]
        
        if activo.get('type') in ['dr', 'fund', 'structured']:
            nombre_limpio = re.split(r'(?i)(\s(inc\.|ltd\.|plc|corp\.|gdr|adr|sponsored))', activo['description'])[0].strip()
            resp_nom = requests.get(url, params={"text": nombre_limpio, "domain": "production"}, headers=headers)
            data_nom = resp_nom.json()
            candidatos = [x for x in data_nom.get('symbols', []) if x.get('type') == 'stock' and x.get('isin') != isin_origen]
            if candidatos: activo = candidatos[0]

        return activo['symbol'], activo['country'], activo['exchange'], activo.get('isin')
    except:
        return None, None, None, None

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
            r = requests.get(f"https://{env}.trading212.com/api/v0/equity/metadata/exchanges", headers=headers, timeout=5)
            if r.status_code == 200: return f"https://{env}.trading212.com"
        except: pass
    return None

def process_portfolio(base_url, api_key, api_secret):
    headers = get_auth_header(api_key, api_secret)
    url = f"{base_url}/api/v0/equity/positions"

    response = requests.get(url, headers=headers)
    
    # Manejo de error de autenticación explícito
    if response.status_code == 401:
        raise ValueError("Credenciales de Trading212 inválidas.")
    if response.status_code != 200:
        raise Exception(f"Error de API Trading212: {response.status_code}")

    positions = response.json()
    # Si devuelve lista vacía o error
    if not isinstance(positions, list):
         return {"summary": {"total_portfolio_value": 0, "positions_count": 0}, "positions": []}

    total_value = sum(p.get('walletImpact', {}).get('currentValue', 0) for p in positions)
    portfolio_data = []

    for p in positions:
        try:
            inst = p.get('instrument', {})
            ticker_raw = inst.get('ticker', 'UNKNOWN')
            isin_t212 = inst.get('isin', 'N/A')
            ticker_clean = ticker_raw.split('_')[0]

            # 1. TV Intelligence
            sym, pais, exch, isin_main = buscar_mercado_principal_tv(isin_t212)
            ticker_yahoo_calc = convertir_a_ticker_yahoo(sym, pais)
            
            # 2. Yahoo Data
            isin_busqueda = isin_main if isin_main else isin_t212
            r, ticker_real_usado = obtener_datos_yahoo(ticker_yahoo_calc, isin_respaldo=isin_busqueda)
            
            # 3. Rating
            analyst_rating_calc = calcular_consenso_ponderado(
                r['strong_buy'], r['buy'], r['neutral'], r['sell'], r['strong_sell']
            )
            
            pos_data = {
                "ticker": ticker_clean,
                "full_ticker": ticker_raw,
                "isin_origen": isin_t212,
                "name": inst.get('name', 'Unknown'),
                "quantity": p.get('quantity', 0),
                "current_value": round(p.get('walletImpact', {}).get('currentValue', 0), 2),
                "portfolio_percentage": round((p.get('walletImpact', {}).get('currentValue', 0) / total_value * 100), 2) if total_value else 0,
                
                "isin_principal": isin_main if isin_main else isin_t212,
                "ticker_Mercado_principal": ticker_real_usado if ticker_real_usado else "N/A",
                "analyst_rating": analyst_rating_calc,
                "recommendations": r
            }
            portfolio_data.append(pos_data)
            time.sleep(0.05) # Pequeño delay para no saturar APIs
        except Exception:
            # Si falla una posición individual, no rompemos todo el loop, seguimos con la siguiente
            continue

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
            # Si no detecta entorno, las credenciales están mal o T212 está caído
            return {"status": "error", "message": "No se pudo autenticar. Verifique API Key y Secret."}

        # 2. Procesar datos
        data = process_portfolio(base_url, creds.api_key, creds.api_secret)
        
        return {"status": "success", "data": data}

    except ValueError as ve:
        # Error controlado (ej. credenciales malas)
        return {"status": "error", "message": str(ve)}
        
    except Exception as e:
        # Error general (cualquier crash inesperado)
        return {"status": "error", "message": f"Error interno del servidor: {str(e)}"}

# Para ejecutar localmente si se corre el script directamente
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)