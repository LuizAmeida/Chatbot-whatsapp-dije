const { default: makeWASocket, useMultiFileAuthState, DisconnectReason } = require('@whiskeysockets/baileys');
const qrcode = require('qrcode-terminal');
const axios = require('axios');
const pino = require('pino');

const WEBHOOK_URL = 'http://127.0.0.1:8000/webhook';
const WEBHOOK_SECRET = 'nexus_secret_token_seguro_2026';

// Mapa em memória para relacionar LIDs a Telefones Reais
const contactsMap = new Map();

async function startBot() {
    const { state, saveCreds } = await useMultiFileAuthState('auth_info_baileys');

    const sock = makeWASocket({
        auth: state,
        logger: pino({ level: 'silent' }),
        syncFullHistory: false
    });

    sock.ev.on('creds.update', saveCreds);

    // Salva mapeamento de contatos recebidos do WhatsApp
    sock.ev.on('contacts.upsert', (contacts) => {
        for (const c of contacts) {
            if (c.id && c.lid) {
                contactsMap.set(c.lid, c.id.split('@')[0]);
            }
            if (c.id && c.phoneNumber) {
                contactsMap.set(c.id, c.phoneNumber);
            }
        }
    });

    sock.ev.on('connection.update', (update) => {
        const { connection, lastDisconnect, qr } = update;

        if (qr) {
            console.log('\n--- ESCANEIE O QR CODE COM SEU WHATSAPP ---\n');
            qrcode.generate(qr, { small: true });
        }

        if (connection === 'close') {
            const shouldReconnect = lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut;
            console.log('Conexão encerrada. Reconectando...', shouldReconnect);
            if (shouldReconnect) startBot();
        } else if (connection === 'open') {
            console.log('✅ WhatsApp conectado com sucesso!');
        }
    });

    sock.ev.on('messages.upsert', async (m) => {
        const msg = m.messages[0];
        if (!msg || msg.key.fromMe || m.type !== 'notify') return;

        const remoteJid = msg.key.remoteJid;
        
        // Ignora grupos, canais e transmissões
        if (remoteJid.includes('@g.us') || remoteJid.includes('@newsletter') || remoteJid.includes('status@broadcast')) {
            return;
        }

        // Resolução do número de telefone real
        let realPhone = '';
        if (remoteJid.includes('@lid')) {
            realPhone = contactsMap.get(remoteJid) || '';
            if (!realPhone && msg.key.participantPn) {
                realPhone = msg.key.participantPn.split('@')[0];
            }
        } else {
            realPhone = remoteJid.split('@')[0];
        }

        const text = 
            msg.message?.conversation || 
            msg.message?.extendedTextMessage?.text || 
            msg.message?.imageMessage?.caption ||
            '';

        if (text) {
            console.log(`\n📩 Mensagem de [${remoteJid}] | Número Real: [${realPhone || 'Não mapeado'}]: ${text}`);

            try {
                // Ativa status digitando no chat
                await sock.readMessages([msg.key]);
                await sock.sendPresenceUpdate('composing', remoteJid);

                const response = await axios.post(
                    WEBHOOK_URL,
                    {
                        from: remoteJid,
                        sender: remoteJid,
                        phone: realPhone,
                        message: text,
                        body: text,
                        text: text
                    },
                    {
                        headers: {
                            'x-webhook-secret': WEBHOOK_SECRET
                        }
                    }
                );

                console.log('Resposta recebida do FastAPI:', response.data);

                // Se o backend retornou que o contato está na whitelist ignorada, apenas pausa e não responde
                if (response.data?.status === 'ignorado_whitelist' || response.data?.status === 'em_atendimento_humano') {
                    await sock.sendPresenceUpdate('paused', remoteJid);
                    return;
                }

                const reply = 
                    response.data?.resposta || 
                    response.data?.reply || 
                    response.data?.response || 
                    response.data?.message;
                
                if (reply) {
                    await sock.sendPresenceUpdate('paused', remoteJid);
                    await sock.sendMessage(remoteJid, { text: String(reply) });
                    console.log(`📤 Resposta enviada para ${remoteJid}!`);
                } else {
                    await sock.sendPresenceUpdate('paused', remoteJid);
                }
            } catch (err) {
                console.error('❌ Erro no processamento:', err.response?.data || err.message);
                await sock.sendPresenceUpdate('paused', remoteJid);
            }
        }
    });
}

startBot();