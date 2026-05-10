# bot-aapanel-deploy 🤖

Bot Telegram para deploy de sites no aaPanel.

**Fluxo interativo:** uma pergunta por vez, servidor primeiro, detecta git e BD automaticamente.

## Instalação no servidor

```bash
# 1. Clonar
cd /bots
git clone https://github.com/by-lua/bot-aapanel-deploy.git
cd bot-aapanel-deploy

# 2. Criar .env
cp .env.example .env
# Edite .env com o token do bot e seu ID do Telegram

# 3. Venv + deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 4. Systemd
cp bot-aapanel-deploy.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable bot-aapanel-deploy
systemctl start bot-aapanel-deploy

# Ver logs
journalctl -u bot-aapanel-deploy -f
```

## Auto-update

Todo `git push` na main:
1. GitHub Action SSH no servidor
2. `git pull`
3. Reinstala deps
4. Restart via systemctl

## Comandos

- `/start` — Iniciar fluxo interativo
- `/restart` — Reiniciar o bot (apenas admins)

## Segurança

- `.env` com token NÃO está no git
- `ALLOWED_USERS` limita quem pode usar
- Dados por usuário ficam isolados em `~/.aapanel-deploy/{user_id}/`
- Tokens de API mascarados nos logs
