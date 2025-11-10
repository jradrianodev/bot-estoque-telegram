import os
import json
import gspread
import requests
import datetime
from flask import Flask, request, jsonify

# --- CONFIGURAÇÃO ---
# (Vamos buscar do Render, mas pode deixar os valores antigos aqui por segurança)
BOT_TOKEN = os.environ.get('BOT_TOKEN', "8429737414:AAEu2MZwc7AaNj7XScU9tRX_HyiIP5f-9Zw")
SHEET_ID = os.environ.get('SHEET_ID', "13Nr2zfXBhRxFpsC5zfhHGAkrdrISxvApjX9KgUwvAsk")
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', "AIzaSyAutlE8Zg4b2oIqbe5wYd1TwNfqLa-uEgI")
# --- FIM DA CONFIGURAÇÃO ---

# Inicializa o Flask (nosso servidor)
app = Flask(__name__)

# Conecta ao Google Sheets
# O Render vai criar o arquivo 'credentials.json' para nós
try:
    gc = gspread.service_account(filename='credentials.json')
    spreadsheet = gc.open_by_key(SHEET_ID)
    aba_historico = spreadsheet.worksheet("Histórico")
    aba_produtos = spreadsheet.worksheet("Produtos")
    print("Conectado ao Google Sheets com sucesso.")
except Exception as e:
    print(f"Erro ao conectar ao Google Sheets: {e}")

# Cache simples em memória para evitar duplicatas
processed_ids = set()

# ===============================================================
# HELPER: CHAMADA DA IA (GEMINI)
# ===============================================================
def get_ia_data(texto, produtos_lista):
    print(f"Chamando IA para: {texto}")
    prompt = f"""
    Você é um assistente de estoque. Sua tarefa é analisar uma frase e extrair UMA LISTA de todos os produtos, quantidades e setor.

    LISTA DE PRODUTOS VÁLIDOS:
    {produtos_lista}

    FRASE DO USUÁRIO: "{texto}"

    REGRAS:
    1. 'descricao' DEVE ser o nome EXATO da lista. Use correspondência aproximada para encontrar.
    2. O 'setor' é o local/departamento (ex: 'limpeza', 'clínica veterinária', 'copa', 'NPJ'). Ele pode ser mencionado apenas uma vez e deve ser aplicado a TODOS os itens da lista.
    3. Se o setor não for mencionado, use "Não Informado" para TODOS.
    4. 'quantidade' DEVE ser um número (ex: "01" vira "1").

    Retorne APENAS um array de objetos JSON no formato:
    [
      {{"descricao": "NOME EXATO DO ITEM 1", "quantidade": "NUMERO 1", "setor": "SETOR APLICADO"}},
      {{"descricao": "NOME EXATO DO ITEM 2", "quantidade": "NUMERO 2", "setor": "SETOR APLICADO"}}
    ]
    Se NENHUM produto da lista for encontrado, retorne um array vazio: []
    """
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {'Content-Type': 'application/json'}
    payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}]})
    
    response = requests.post(url, headers=headers, data=payload)
    
    if response.status_code != 200:
        raise Exception(f"Erro da API Gemini: {response.text}")

    result = response.json()
    texto_gerado = result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '[]')
    texto_gerado = texto_gerado.replace("```json", "").replace("```", "").strip()
    
    lista_de_itens = json.loads(texto_gerado)
    
    if not isinstance(lista_de_itens, list) or len(lista_de_itens) == 0:
        raise Exception("Nenhum produto da lista foi encontrado na sua mensagem.")
        
    return lista_de_itens

# ===============================================================
# HELPER: BUSCAR DADOS (LOOKUP)
# ===============================================================
def get_lookup_map():
    print("Buscando lista de produtos na planilha...")
    produtos_data = aba_produtos.get_all_values()[1:] # Pula o cabeçalho
    produtos_map = {}
    for row in produtos_data:
        if row[0]: # Coluna A (PRODUTO)
            produtos_map[row[0]] = {
                "material": row[1] or "",  # B - CÓDIGO
                "conta":    row[2] or "",  # C - CONTA
                "num_conta":row[3] or "",  # D - NUM_CONTA
                "deposito": row[4] or ""   # E - DEPOSITO
            }
    return produtos_map

# ===============================================================
# HELPER: ENVIAR MENSAGEM TELEGRAM
# ===============================================================
def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        requests.post(url, json=payload)
        print(f"Resposta enviada para {chat_id}")
    except Exception as e:
        print(f"Erro ao enviar mensagem: {e}")

# ===============================================================
# O WEBHOOK (O NOVO "PORTEIRO")
# ===============================================================
@app.route('/webhook', methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    
    try:
        update_id = update.get('update_id')
        message = update.get('message')
        
        if not message or not message.get('text') or not update_id:
            return jsonify(status="ok") # Ignora

        # --- PREVENÇÃO DE DUPLICIDADE ---
        if update_id in processed_ids:
            print(f"Ignorando ID duplicado: {update_id}")
            return jsonify(status="ok")
        
        if len(processed_ids) > 1000: # Limpa o cache
            processed_ids.clear()
        processed_ids.add(update_id)
        # ---------------------------------
        
        chat_id = message['chat']['id']
        text = message['text']
        print(f"Recebida nova mensagem: {text}")

        # 1. Busca dados da planilha
        produtos_map = get_lookup_map()
        
        # 2. Chama IA
        lista_de_itens = get_ia_data(text, "\n".join(produtos_map.keys()))
        
        # 3. Prepara linhas para a planilha
        linhas_para_adicionar = []
        respostas_telegram = []
        data_atual = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")
        
        for item in lista_de_itens:
            lookup = produtos_map.get(item['descricao'], {})
            
            linhas_para_adicionar.append([
                data_atual,
                item['descricao'],
                item['quantidade'],
                item['setor'],
                lookup.get('deposito', ''),
                lookup.get('conta', ''),
                lookup.get('num_conta', ''),
                lookup.get('material', '')
            ])
            respostas_telegram.append(f"📦 {item['descricao']} (Qtd: {item['quantidade']})")

        # 4. Escreve na planilha (em lote)
        if linhas_para_adicionar:
            aba_historico.append_rows(linhas_para_adicionar)
            print(f"{len(linhas_para_adicionar)} linhas adicionadas à planilha.")
        
        # 5. Envia resposta
        setor = lista_de_itens[0]['setor']
        resposta_final = f"✅ Lançados {len(lista_de_itens)} itens para o setor \"{setor}\"!\n\n" + "\n".join(respostas_telegram)
        send_telegram_message(chat_id, resposta_final)

    except Exception as e:
        print(f"Erro no processamento: {e}")
        try:
            chat_id = update['message']['chat']['id']
            send_telegram_message(chat_id, f"❌ Ocorreu um erro no processamento:\n{e}")
        except:
            pass 

    # Responde OK ao Telegram
    return jsonify(status="ok")

# ===============================================================
# ROTA DE "HEALTH CHECK" (para o Render)
# ===============================================================
@app.route('/')
def health_check():
    return "Bot está vivo!", 200

# O Gunicorn (do Render) vai rodar isso, não precisamos do app.run()