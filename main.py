import os
import json
import gspread
import requests
import datetime
import traceback
import re
from flask import Flask, request, jsonify

# --- CONFIGURAÇÃO ---
BOT_TOKEN = os.environ.get('BOT_TOKEN', "8429737414:AAEu2MZwc7AaNj7XScU9tRX_HyiIP5f-9Zw")
SHEET_ID = os.environ.get('SHEET_ID', "13Nr2zfXBhRxFpsC5zfhHGAkrdrISxvApjX9KgUwvAsk")
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', "AIzaSyAutlE8Zg4b2oIqbe5wYd1TwNfqLa-uEgI")
# --- FIM DA CONFIGURAÇÃO ---

app = Flask(__name__)

# Conecta ao Google Sheets
try:
    gc = gspread.service_account(filename='credentials.json')
    spreadsheet = gc.open_by_key(SHEET_ID)
    aba_historico = spreadsheet.worksheet("Histórico")
    aba_produtos = spreadsheet.worksheet("Produtos")
    print("Conectado ao Google Sheets com sucesso.")
except Exception as e:
    print("--- ERRO REAL DE CONEXÃO ---")
    print(traceback.format_exc())
    print("--- FIM DO ERRO ---")

processed_ids = set()

# ===============================================================
# HELPER: CHAMADA DA IA
# ===============================================================
def get_ia_data(texto, produtos_lista):
    print(f"Chamando IA para: {texto}")
    
    prompt = f"""
    Você é um sistema de extração de dados.
    Analise a frase do usuário e extraia os produtos baseados na lista permitida.

    LISTA DE PRODUTOS VÁLIDOS:
    {produtos_lista}

    FRASE DO USUÁRIO: "{texto}"

    REGRAS:
    1. 'descricao': Deve ser o nome EXATO que está na lista acima.
    2. 'setor': Identifique o setor. Se não houver, use "Não Informado".
    3. 'quantidade': Converta para número inteiro.

    SAÍDA: Retorne APENAS um JSON (Array de Objetos).
    [
      {{"descricao": "NOME DO ITEM", "quantidade": 1, "setor": "Setor A"}}
    ]
    """
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-exp:generateContent?key={GEMINI_API_KEY}"
    
    headers = {'Content-Type': 'application/json'}
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]})
    
    response = requests.post(url, headers=headers, data=payload)
    
    if response.status_code != 200:
        raise Exception(f"Erro da API Gemini: {response.text}")

    result = response.json()
    
    try:
        texto_gerado = result['candidates'][0]['content']['parts'][0]['text']
    except (KeyError, IndexError):
        print(f"DEBUG IA (Vazio): {result}")
        return []

    print(f"DEBUG IA RAW: {texto_gerado}") 

    match = re.search(r'\[.*\]', texto_gerado, re.DOTALL)
    
    if match:
        json_limpo = match.group(0)
        try:
            lista_de_itens = json.loads(json_limpo)
            return lista_de_itens
        except json.JSONDecodeError:
            raise Exception(f"A IA retornou algo parecido com JSON, mas estava quebrado: {json_limpo}")
    else:
        texto_limpo = texto_gerado.replace("```json", "").replace("```", "").strip()
        if not texto_limpo:
             return []
        try:
            return json.loads(texto_limpo)
        except:
            raise Exception(f"A IA não retornou um JSON válido. Retornou: {texto_gerado}")

# ===============================================================
# HELPER: BUSCAR DADOS
# ===============================================================
def get_lookup_map():
    print("Buscando lista de produtos na planilha...")
    produtos_data = aba_produtos.get_all_values()[1:]
    produtos_map = {}
    for row in produtos_data:
        if row[0]:
            row += [""] * (5 - len(row))
            produtos_map[row[0]] = {
                "material": row[1],
                "conta":    row[2],
                "num_conta":row[3],
                "deposito": row[4]
            }
    return produtos_map

# ===============================================================
# HELPER: ENVIAR MENSAGEM
# ===============================================================
def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Erro ao enviar mensagem: {e}")

# ===============================================================
# O WEBHOOK
# ===============================================================
@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    
    try:
        update_id = update.get('update_id')
        message = update.get('message')
        
        if not message or not message.get('text') or not update_id:
            return jsonify(status="ok")

        if update_id in processed_ids:
            print(f"Ignorando ID duplicado: {update_id}")
            return jsonify(status="ok")
        
        if len(processed_ids) > 1000:
            processed_ids.clear()
        processed_ids.add(update_id)
        
        chat_id = message['chat']['id']
        text = message['text']
        print(f"Recebida nova mensagem: {text}")

        produtos_map = get_lookup_map()
        
        try:
            lista_de_itens = get_ia_data(text, "\n".join(produtos_map.keys()))
        except Exception as e:
            send_telegram_message(chat_id, f"⚠️ A IA se confundiu: {e}")
            return jsonify(status="ok")
        
        if not lista_de_itens:
             send_telegram_message(chat_id, "⚠️ Não identifiquei produtos da lista.")
             return jsonify(status="ok")

        linhas_para_adicionar = []
        respostas_telegram = []
        data_atual = datetime.datetime.now().strftime("%d/%m/%Y")
        
        setor_geral = "Não Informado"
        if len(lista_de_itens) > 0:
            setor_geral = lista_de_itens[0].get('setor', 'Não Informado')

        for item in lista_de_itens:
            nome_item = item.get('descricao')
            qtd_item = item.get('quantidade')
            lookup = produtos_map.get(nome_item, {})
            
            linhas_para_adicionar.append([
                data_atual, nome_item, qtd_item, setor_geral,
                lookup.get('deposito', ''), lookup.get('conta', ''),
                lookup.get('num_conta', ''), lookup.get('material', '')
            ])
            respostas_telegram.append(f"📦 {nome_item} (Qtd: {qtd_item})")

        # 4. Escreve na planilha (Lógica com Auto-Expansão de Linhas)
        if linhas_para_adicionar:
            coluna_a = aba_historico.col_values(1) 
            proxima_linha = len(coluna_a) + 1
            total_linhas_novas = len(linhas_para_adicionar)
            
            # Verifica se precisa criar mais linhas na planilha
            linhas_totais_planilha = aba_historico.row_count
            linhas_necessarias = proxima_linha + total_linhas_novas - 1
            
            if linhas_necessarias > linhas_totais_planilha:
                linhas_para_criar = linhas_necessarias - linhas_totais_planilha + 10
                aba_historico.add_rows(linhas_para_criar)
                print(f"Criadas {linhas_para_criar} novas linhas na planilha.")

            values_str = [[str(x) for x in linha] for linha in linhas_para_adicionar]
            range_name = f"A{proxima_linha}:H{proxima_linha + total_linhas_novas - 1}"
            
            aba_historico.update(range_name=range_name, values=values_str)
            print(f"Adicionado na linha {proxima_linha}.")
        
        resposta_final = f"✅ Lançados {len(lista_de_itens)} itens para \"{setor_geral}\"!\n\n" + "\n".join(respostas_telegram)
        send_telegram_message(chat_id, resposta_final)

    except Exception as e:
        print(f"Erro no processamento: {e}")
        traceback.print_exc()
        try:
            chat_id = update['message']['chat']['id']
            send_telegram_message(chat_id, f"❌ Ocorreu um erro: {e}")
        except:
            pass

    return jsonify(status="ok")

@app.route('/')
def health_check():
    return "Bot está vivo!", 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)