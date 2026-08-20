import os
import csv
import json
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Any
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel
import uvicorn
from dotenv import load_dotenv
import google.generativeai as genai

# -------------------------------------------------------------
# Carregamento Seguro das Variáveis de Ambiente
# -------------------------------------------------------------
load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
WEBHOOK_SECRET_TOKEN = os.getenv("WEBHOOK_SECRET_TOKEN", "nexus_secret_token_seguro_2026")

if not GEMINI_API_KEY:
    print("[ALERTA DE SEGURANÇA] Chave GEMINI_API_KEY não encontrada no arquivo .env!")

genai.configure(api_key=GEMINI_API_KEY)

# -------------------------------------------------------------
# Leitura Dinâmica do Arquivo de Configuração do Cliente
# -------------------------------------------------------------
CONFIG_PATH = "config_negocio.json"
CSV_PATH = "agendamentos.csv"
DB_PATH = "chatbot.db"

def carregar_config_negocio() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return {
            "nome_estabelecimento": "Nexus Soluções",
            "nome_proprietaria": "Luiz",
            "whatsapp_proprietaria": "201557735221",
            "localizacao_bairro": "Egito / Brasil",
            "horario_funcionamento": "Segunda a Sábado das 10:00 às 20:00",
            "servicos_e_precos": [],
            "politica_agendamento": "Avisar com antecedência.",
            "formas_pagamento": "Dinheiro, InstaPay, Pix e Cartão"
        }
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def montar_system_prompt() -> str:
    cfg = carregar_config_negocio()
    servicos_formatados = "\n".join([
        f"• {s['servico']} — Preço: {s['preco']} (Duração aprox: {s['duracao']}) - {s.get('descricao', '')}"
        for s in cfg.get("servicos_e_precos", [])
    ])
    
    return (
        f"Você é a Sofia, atendente virtual e consultora exclusiva de '{cfg['nome_estabelecimento']}'.\n"
        f"Responsável: {cfg['nome_proprietaria']}.\n"
        f"Localização/Bairro: {cfg['localizacao_bairro']}.\n"
        f"Horário de Atendimento: {cfg['horario_funcionamento']}.\n"
        f"Formas de Pagamento: {cfg['formas_pagamento']}.\n"
        f"Políticas de Agendamento: {cfg['politica_agendamento']}.\n\n"
        "CATÁLOGO DE SERVIÇOS E PREÇOS:\n"
        f"{servicos_formatados}\n\n"
        "DIRETRIZES DE ATENDIMENTO E VENDAS:\n"
        "1. Você é poliglota nativa em Português, Árabe (Egípcio/Padrão/Franco), Inglês, Espanhol, Francês e Italiano.\n"
        "2. Responda SEMPRE no mesmo idioma utilizado pelo cliente.\n"
        "3. Tire dúvidas com simpatia e conduza o cliente para o AGENDAMENTO, envio do VÍDEO DEMONSTRATIVO da máquina ou LINK DE PAGAMENTO.\n"
        "4. Sempre que a cliente confirmar um horário (dia e hora), faça uma confirmação clara e amigável dos detalhes.\n"
        "5. Mantenha mensagens concisas e acolhedoras para WhatsApp."
    )

IGNORAR_REMETENTES = [
    "5585997381757", "5585996945621", "201557735221", "5585999337626", 
    "201145631000", "5585997872203", "5585996511398", "201157648198", 
    "201125555318", "201027711831", "201027184056", "5561999095625",
    "136227906945091", "9569489182749"
]

TERMOS_TRANSBORDO_HUMANO = [
    "humano", "atendente", "falar com alguem", "pessoa real", 
    "suporte humano", "falar com atendente", "representante",
    "human", "talk to agent", "speak to human",
    "انسان", "خدمة العملاء", "تحدث مع موظف", "شخص حقيقي"
]

# MELHORIA DE ESCALA: Priorizando modelos ultra-otimizados e de baixo custo (Flash-Lite / Flash)
MODELOS_CANDIDATOS = [
    "models/gemini-3.5-flash-lite",
    "models/gemini-3.5-flash",
    "models/gemini-flash-latest",
    "models/gemini-3.7-flash"
]

# -------------------------------------------------------------
# Camada de Dados: SQLite & Planilha CSV com Migração Automática
# -------------------------------------------------------------
def inicializar_banco_e_planilha():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # 1. Histórico de Conversas
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historico_conversas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            remetente TEXT NOT NULL,
            role TEXT NOT NULL,
            conteudo TEXT NOT NULL,
            data_hora DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 2. Status dos Usuários
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS status_usuarios (
            remetente TEXT PRIMARY KEY,
            atendimento_humano INTEGER DEFAULT 0,
            silenciado_offtopic INTEGER DEFAULT 0,
            atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # MIGRAÇÃO AUTOMÁTICA: Adiciona a coluna silenciado_offtopic se ela não existir
    try:
        cursor.execute("ALTER TABLE status_usuarios ADD COLUMN silenciado_offtopic INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # 3. Tabela de Agendamentos Fechados
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS agendamentos_fechados (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            remetente TEXT NOT NULL,
            servico TEXT,
            data_hora_agendada TEXT,
            detalhes_traduzidos TEXT,
            criado_em DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

    # Criação do cabeçalho da Planilha CSV se não existir
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["Data Registro", "Cliente (WhatsApp)", "Serviço", "Data e Horário Marcado", "Notas / Tradução"])

inicializar_banco_e_planilha()

def salvar_agendamento_planilha(remetente: str, servico: str, data_hora: str, notas: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO agendamentos_fechados (remetente, servico, data_hora_agendada, detalhes_traduzidos)
        VALUES (?, ?, ?, ?)
    """, (remetente, servico, data_hora, notas))
    conn.commit()
    conn.close()

    agora = datetime.now().strftime("%d/%m/%Y %H:%M")
    with open(CSV_PATH, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([agora, remetente, servico, data_hora, notas])
    print(f"📊 [PAINEL/CSV] Agendamento registrado com sucesso em {CSV_PATH}")

def obter_status_usuario(identificador: str) -> Dict[str, int]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT atendimento_humano, silenciado_offtopic FROM status_usuarios WHERE remetente = ?", (identificador,))
    linha = cursor.fetchone()
    conn.close()
    if linha:
        return {"atendimento_humano": linha[0], "silenciado_offtopic": linha[1]}
    return {"atendimento_humano": 0, "silenciado_offtopic": 0}

def atualizar_status_usuario(identificador: str, humano: Optional[int] = None, silenciado: Optional[int] = None):
    status_atual = obter_status_usuario(identificador)
    novo_humano = humano if humano is not None else status_atual["atendimento_humano"]
    novo_silenciado = silenciado if silenciado is not None else status_atual["silenciado_offtopic"]

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO status_usuarios (remetente, atendimento_humano, silenciado_offtopic, atualizado_em)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(remetente) DO UPDATE SET 
            atendimento_humano = excluded.atendimento_humano,
            silenciado_offtopic = excluded.silenciado_offtopic,
            atualizado_em = CURRENT_TIMESTAMP
    """, (identificador, novo_humano, novo_silenciado))
    conn.commit()
    conn.close()

def obter_historico_db(remetente: str, limite: int = 14) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT role, conteudo FROM historico_conversas
        WHERE remetente = ?
        ORDER BY id DESC LIMIT ?
    """, (remetente, limite))
    linhas = cursor.fetchall()
    conn.close()

    linhas.reverse()
    return [{"role": r, "parts": [c]} for r, c in linhas]

def salvar_mensagem_db(remetente: str, role: str, conteudo: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO historico_conversas (remetente, role, conteudo)
        VALUES (?, ?, ?)
    """, (remetente, role, conteudo))
    conn.commit()
    conn.close()

def classificar_intencao_mensagem(mensagem: str) -> str:
    prompt_classificacao = (
        "Classifique a mensagem do usuário em apenas UMA palavra:\n"
        "1. 'COMERCIAL' -> Dúvidas de procedimentos, valores, agendamentos, localização, saudações (oi, ola, salam, hi, etc).\n"
        "2. 'OFFTOPIC' -> Conversa puramente pessoal/familiar desconexa (ex: papai, cadê você, família).\n\n"
        f"Mensagem: \"{mensagem}\"\n\n"
        "Responda apenas 'COMERCIAL' ou 'OFFTOPIC'."
    )
    for modelo_nome in MODELOS_CANDIDATOS:
        try:
            m = genai.GenerativeModel(model_name=modelo_nome)
            resp = m.generate_content(prompt_classificacao).text.strip().upper()
            if "OFFTOPIC" in resp:
                return "OFFTOPIC"
            return "COMERCIAL"
        except Exception:
            continue
    return "COMERCIAL"

def analisar_e_extrair_agendamento(remetente: str) -> Optional[Dict[str, str]]:
    historico = obter_historico_db(remetente, limite=6)
    if len(historico) < 2:
        return None
    
    conversa_texto = "\n".join([f"{item['role']}: {item['parts'][0]}" for item in historico])
    prompt_extracao = (
        "Você é um analisador de conversas comerciais. Verifique se o cliente acabou de fechar ou confirmar uma data e horário de atendimento/serviço.\n"
        f"Histórico:\n{conversa_texto}\n\n"
        "Se NÃO houve confirmação clara de agendamento, responda: {\"agendado\": false}\n"
        "Se HOUVE confirmação de agendamento/serviço, responda estritamente em JSON com o formato:\n"
        "{\n"
        "  \"agendado\": true,\n"
        "  \"servico\": \"nome do serviço ou procedimento\",\n"
        "  \"data_hora\": \"dia da semana / data e horário\",\n"
        "  \"resumo_portugues\": \"resumo em português das observações ou preferências da cliente\"\n"
        "}"
    )

    for modelo_nome in MODELOS_CANDIDATOS:
        try:
            m = genai.GenerativeModel(model_name=modelo_nome)
            resp_texto = m.generate_content(prompt_extracao).text.strip()
            if "```json" in resp_texto:
                resp_texto = resp_texto.split("```json")[1].split("```")[0].strip()
            elif "```" in resp_texto:
                resp_texto = resp_texto.split("```")[1].split("```")[0].strip()

            dados = json.loads(resp_texto)
            if dados.get("agendado") is True:
                return dados
        except Exception:
            continue
    return None

# -------------------------------------------------------------
# Motor de Processamento Otimizado
# -------------------------------------------------------------
def processar_com_gemini(remetente: str, mensagem_usuario: str) -> Dict[str, Any]:
    msg_low = mensagem_usuario.lower()

    if "#bot_on" in msg_low:
        atualizar_status_usuario(remetente, humano=0, silenciado=0)
        return {
            "resposta": "🤖 [Sistema]: Sofia reativada com sucesso para este contato.",
            "alerta_admin": None
        }

    if any(termo in msg_low for termo in TERMOS_TRANSBORDO_HUMANO):
        atualizar_status_usuario(remetente, humano=1, silenciado=0)
        salvar_mensagem_db(remetente, "user", mensagem_usuario)
        
        if any(char in mensagem_usuario for char in "أبتثجحخدذرزسشصضطظعغفقكلمنهوي"):
            resp = "تمام جداً يا فندم! تم تحويل المحادثة لأحد ممثلينا، وهيتم التواصل مع حضرتك فوراً. شكراً لتواصلك! 😊"
        elif any(k in msg_low for k in ["human", "agent", "support"]):
            resp = "Understood! I'm transferring you to our team right now. Someone will reach out to you shortly. Thank you! 😊"
        else:
            resp = "Perfeito! Já transferi seu atendimento para nossa equipe. Em breve um de nossos consultores responderá por aqui. Muito obrigado! 😊"

        salvar_mensagem_db(remetente, "model", resp)
        return {"resposta": resp, "alerta_admin": None}

    historico = obter_historico_db(remetente)
    conteudo_requisicao = historico + [{"role": "user", "parts": [mensagem_usuario]}]
    system_prompt_atualizado = montar_system_prompt()

    for modelo_nome in MODELOS_CANDIDATOS:
        try:
            print(f"[GEMINI] Processando com modelo otimizado: {modelo_nome}...")
            try:
                modelo_instanciado = genai.GenerativeModel(
                    model_name=modelo_nome,
                    system_instruction=system_prompt_atualizado
                )
            except Exception:
                modelo_instanciado = genai.GenerativeModel(model_name=modelo_nome)

            resposta = modelo_instanciado.generate_content(conteudo_requisicao)
            texto_gerado = resposta.text.strip()

            if texto_gerado:
                salvar_mensagem_db(remetente, "user", mensagem_usuario)
                salvar_mensagem_db(remetente, "model", texto_gerado)

                alerta_admin = None
                dados_agendamento = analisar_e_extrair_agendamento(remetente)
                if dados_agendamento:
                    servico = dados_agendamento.get("servico", "Atendimento")
                    data_hora = dados_agendamento.get("data_hora", "A combinar")
                    resumo = dados_agendamento.get("resumo_portugues", "Sem observações adicionais.")

                    salvar_agendamento_planilha(remetente, servico, data_hora, resumo)

                    alerta_admin = (
                        "🔔 *NOVO AGENDAMENTO CONFIRMADO!*\n\n"
                        f"👤 *Cliente:* {remetente}\n"
                        f"💅 *Serviço:* {servico}\n"
                        f"⏰ *Data/Horário:* {data_hora}\n"
                        f"📝 *Detalhes:* {resumo}\n\n"
                        "_Registrado automaticamente no painel de agendamentos._"
                    )
                    print(f"\n🔥 [DISPARANDO ALERTA PARA O ADMIN]:\n{alerta_admin}\n")

                return {"resposta": texto_gerado, "alerta_admin": alerta_admin}

        except Exception as e:
            print(f"[AVISO COTA/MODELO {modelo_nome}]: {e}")
            continue

    salvar_mensagem_db(remetente, "user", mensagem_usuario)
    cfg = carregar_config_negocio()
    resp = f"Olá! Seja bem-vindo(a) ao {cfg['nome_estabelecimento']}! 😊 Como posso te ajudar com nossos serviços hoje?"
    salvar_mensagem_db(remetente, "model", resp)
    return {"resposta": resp, "alerta_admin": None}

# -------------------------------------------------------------
# Servidor FastAPI
# -------------------------------------------------------------
app = FastAPI(title="Nexus Soluções - WhatsApp AI Backend Multi-Language (Otimizado)")

class WhatsAppPayload(BaseModel):
    from_user: Optional[str] = None
    sender: Optional[str] = None
    phone: Optional[str] = None
    message: Optional[str] = None
    body: Optional[str] = None
    text: Optional[str] = None

    class Config:
        extra = "allow"

@app.get("/")
def home():
    cfg = carregar_config_negocio()
    return {
        "status": "online",
        "estabelecimento": cfg.get("nome_estabelecimento"),
        "admin_alert": "Enabled",
        "csv_panel": "agendamentos.csv Active",
        "arquitetura": "Otimizada (Flash-Lite / Scale Ready)"
    }

@app.post("/webhook")
async def receber_mensagem(
    payload: dict,
    x_webhook_secret: Optional[str] = Header(None)
):
    if x_webhook_secret != WEBHOOK_SECRET_TOKEN:
        raise HTTPException(
            status_status=status.HTTP_401_UNAUTHORIZED,
            detail="Acesso não autorizado."
        )

    remetente = str(
        payload.get("sender") or 
        payload.get("from") or 
        payload.get("from_user") or 
        ""
    ).strip()
    
    telefone_real = str(payload.get("phone") or "").strip()
    
    mensagem_texto = str(
        payload.get("message") or 
        payload.get("body") or 
        payload.get("text") or 
        ""
    ).strip()

    if not mensagem_texto:
        return {"status": "ignorado", "resposta": "", "alerta_admin": None}

    # 1. Filtro Whitelist
    for ignorado in IGNORAR_REMETENTES:
        if ignorado and ((telefone_real and ignorado in telefone_real) or (ignorado in remetente)):
            print(f"[BLOQUEIO WHITELIST] Mensagem ignorada: {telefone_real or remetente}")
            return {"status": "ignorado_whitelist", "resposta": "", "alerta_admin": None}

    chave_busca = telefone_real if telefone_real and telefone_real != "Não mapeado" else remetente
    status_contato = obter_status_usuario(chave_busca)

    # 2. Atendimento Humano Ativo
    if status_contato["atendimento_humano"] == 1 and "#bot_on" not in mensagem_texto.lower():
        print(f"[SILÊNCIO - ATENDIMENTO HUMANO ATIVO]: {chave_busca}")
        return {"status": "em_atendimento_humano", "resposta": "", "alerta_admin": None}

    # 3. Classificação Semântica
    intencao = classificar_intencao_mensagem(mensagem_texto)

    if intencao == "COMERCIAL" and status_contato["silenciado_offtopic"] == 1:
        atualizar_status_usuario(chave_busca, silenciado=0)
        status_contato["silenciado_offtopic"] = 0

    if intencao == "OFFTOPIC" and status_contato["silenciado_offtopic"] == 1:
        return {"status": "silenciado_offtopic", "resposta": "", "alerta_admin": None}

    if intencao == "OFFTOPIC" and status_contato["silenciado_offtopic"] == 0:
        atualizar_status_usuario(chave_busca, silenciado=1)
        salvar_mensagem_db(chave_busca, "user", mensagem_texto)

        cfg = carregar_config_negocio()
        resposta_aviso = (
            f"Olá! Tudo bem? 😊\n\n"
            f"Este é o canal oficial de atendimento do **{cfg['nome_estabelecimento']}**.\n"
            f"Se você busca informações sobre nossos serviços ou agendamentos, estou à disposição! "
            f"Se o seu contato for estritamente pessoal, seu recado foi registrado para a equipe. Tenha um ótimo dia!"
        )
        salvar_mensagem_db(chave_busca, "model", resposta_aviso)
        return {
            "status": "sucesso",
            "remetente": chave_busca,
            "resposta": resposta_aviso,
            "alerta_admin": None
        }

    print(f"\n[FASTAPI] Processando mensagem de {chave_busca}: {mensagem_texto}")
    resultado = processar_com_gemini(chave_busca, mensagem_texto)
    print(f"[FASTAPI] Resposta enviada:\n{resultado['resposta']}\n")

    cfg = carregar_config_negocio()
    return {
        "status": "sucesso",
        "remetente": chave_busca,
        "resposta": resultado["resposta"],
        "alerta_admin": resultado["alerta_admin"],
        "admin_phone": cfg.get("whatsapp_proprietaria", "")
    }

if __name__ == "__main__":
    uvicorn.run("servidor_whatsapp:app", host="0.0.0.0", port=8000, reload=True)
