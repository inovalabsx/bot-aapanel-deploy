"""
bot-aapanel-deploy — Telegram bot for deploying sites on aaPanel.
One question per prompt. Multi-user with isolated configs.
"""
import os
import sys
import json
import hashlib
import time
import logging
import subprocess
from pathlib import Path
from functools import wraps

import yaml
import requests
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)

load_dotenv()

# ── Logging ──
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config ──
BOT_TOKEN = os.getenv('BOT_TOKEN')
ALLOWED_USERS = [int(u.strip()) for u in os.getenv('ALLOWED_USERS', '').split(',') if u.strip()]
BOT_DATA_DIR = Path(os.getenv('BOT_DATA_DIR', str(Path.home() / '.aapanel-deploy')))
BOT_DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Conversation States ──
(SELECT_SERVER, MAIN_MENU,
    ADD_DOMAIN, ADD_GIT_SOURCE, ADD_GIT_LINK, ADD_GIT_SKIP,
 ADD_DB_PRESENT, ADD_DB_OPT_IN, ADD_DB_ENGINE, ADD_DB_NAME,
 ADD_SSL, ADD_DEPLOY_CHOICE, ADD_RESULT,
 UPDATE_DOMAIN, UPDATE_CHOOSE, UPDATE_EXEC,
 REMOVE_DOMAIN, REMOVE_CONFIRM,
    CONFIG_MENU) = range(19)

# ── Auth decorator ──
def restricted(func):
    """Only allow configured user IDs"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if ALLOWED_USERS and user_id not in ALLOWED_USERS:
            await update.effective_message.reply_text('⛔ Acesso negado.')
            return ConversationHandler.END
        # Ensure per-user data dir
        user_dir = BOT_DATA_DIR / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        context.user_data['user_dir'] = user_dir
        return await func(update, context, *args, **kwargs)
    return wrapper

# ── Helpers ──

def load_user_config(user_dir: Path) -> dict:
    cfg_path = user_dir / 'config.yaml'
    if cfg_path.exists():
        with open(cfg_path) as f:
            return yaml.safe_load(f) or {}
    return {}

def save_user_config(user_dir: Path, cfg: dict):
    with open(user_dir / 'config.yaml', 'w') as f:
        yaml.dump(cfg, f, default_flow_style=False)

def get_servers(user_dir: Path) -> list:
    return list(load_user_config(user_dir).get('servidores', {}).keys())

def get_server_cfg(user_dir: Path, name: str) -> dict:
    return load_user_config(user_dir).get('servidores', {}).get(name, {})

def get_git_accounts(user_dir: Path) -> list:
    cfg = load_user_config(user_dir)
    accs = []
    for plat, users in cfg.get('git', {}).items():
        for user in users:
            accs.append(f'{plat}:{user}')
    return accs

"""
aaPanel API helper
"""
def aapanel_auth(api_key):
    t = str(int(time.time()))
    token = hashlib.md5((t + hashlib.md5(api_key.encode()).hexdigest()).encode()).hexdigest()
    return t, token

def aapanel_request(base_url, api_key, endpoint, action, data=None, method='GET'):
    t, token = aapanel_auth(api_key)
    url = f'{base_url.rstrip("/")}{endpoint}'
    params = {'action': action, 't': t, 'token': token}
    if data:
        params.update(data)
    try:
        if method == 'GET':
            resp = requests.get(url, params=params, verify=False, timeout=15)
        else:
            resp = requests.post(url, data=params, verify=False, timeout=15)
        return resp.json()
    except Exception as e:
        return {'error': str(e)}

def check_domain_exists(server_cfg, domain):
    """Check if domain already exists on aaPanel"""
    api_key = server_cfg.get('aapanel', {}).get('api_key', '')
    base_url = server_cfg.get('aapanel', {}).get('url', '')
    if not api_key or not base_url:
        return None
    result = aapanel_request(base_url, api_key, '/data', 'getData', {'table': 'sites'})
    if isinstance(result, dict) and result.get('status') is True:
        sites = result.get('data', [])
        for site in sites:
            if domain in site.get('name', '') or domain in site.get('domain', ''):
                return site
    return None

def create_aapanel_site(server_cfg, domain):
    api_key = server_cfg.get('aapanel', {}).get('api_key', '')
    base_url = server_cfg.get('aapanel', {}).get('url', '')
    data = {
        'domain': domain,
        'path': f'/www/wwwroot/{domain}',
        'type_id': '0',
        'type': 'static',
        'port': '80',
    }
    return aapanel_request(base_url, api_key, '/site', 'AddSite', data, method='POST')

def create_database(server_cfg, name, engine='mysql'):
    api_key = server_cfg.get('aapanel', {}).get('api_key', '')
    base_url = server_cfg.get('aapanel', {}).get('url', '')
    data = {
        'name': name,
        'codeing': 'utf8mb4' if engine == 'mysql' else 'UTF8',
        'type': engine,
    }
    result = aapanel_request(base_url, api_key, '/database', 'AddDatabase', data, method='POST')
    return result

def set_ssl(server_cfg, domain):
    api_key = server_cfg.get('aapanel', {}).get('api_key', '')
    base_url = server_cfg.get('aapanel', {}).get('url', '')
    data = {'domain': domain, 'type': '1'}
    return aapanel_request(base_url, api_key, '/site', 'SetSSL', data, method='POST')

def create_github_repo(token, repo_name, private=True):
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github.v3+json',
    }
    data = {'name': repo_name, 'private': private, 'auto_init': False}
    resp = requests.post('https://api.github.com/user/repos', headers=headers, json=data, timeout=15)
    return resp.status_code in (201, 422), resp.json()

def detect_framework(project_path: str) -> dict:
    """Detect project framework from files"""
    p = Path(project_path)
    info = {'framework': None, 'has_node': False, 'has_php': False, 'has_python': False,
            'install_cmd': None, 'build_cmd': None, 'migrate_cmd': None, 'port': None}
    
    if (p / 'package.json').exists():
        try:
            pkg = json.loads((p / 'package.json').read_text())
            deps = {**pkg.get('dependencies', {}), **pkg.get('devDependencies', {})}
            info['has_node'] = True
            info['install_cmd'] = 'npm install'
            if 'next' in deps:
                info['framework'] = 'nextjs'
                info['build_cmd'] = 'npm run build'
                info['port'] = 3000
            elif '@nestjs/core' in deps:
                info['framework'] = 'nestjs'
                info['build_cmd'] = 'npm run build'
                info['port'] = 3000
            elif 'nuxt' in deps or 'nuxt3' in deps:
                info['framework'] = 'nuxt'
                info['build_cmd'] = 'npm run build'
                info['port'] = 3000
            elif 'react-scripts' in deps or 'vite' in deps:
                info['framework'] = 'spa'
                info['build_cmd'] = 'npm run build'
        except: pass
    
    if (p / 'composer.json').exists():
        try:
            composer = json.loads((p / 'composer.json').read_text())
            requires = composer.get('require', {})
            info['has_php'] = True
            info['install_cmd'] = 'composer install'
            if 'laravel/framework' in requires:
                info['framework'] = 'laravel'
                info['migrate_cmd'] = 'php artisan migrate'
        except: pass
    
    if (p / 'manage.py').exists():
        info['has_python'] = True
        info['framework'] = 'django'
        info['install_cmd'] = 'pip install -r requirements.txt'
        info['migrate_cmd'] = 'python manage.py migrate'
        info['port'] = 8000
    
    return info

async def deploy_via_ssh(server_cfg, domain, git_url, framework_info):
    """SSH into server and deploy"""
    host = server_cfg.get('host')
    user = server_cfg.get('user', 'root')
    password = server_cfg.get('password')
    
    if not host:
        return '❌ Servidor sem host configurado.'
    
    import paramiko
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username=user, password=password, timeout=10)
        
        cmds = [
            f'cd /www/wwwroot && git clone {git_url} {domain}',
            f'rm -rf /www/wwwroot/{domain}/.git',
        ]
        
        if framework_info.get('install_cmd'):
            cmds.append(f'cd /www/wwwroot/{domain} && {framework_info["install_cmd"]}')
        if framework_info.get('build_cmd'):
            cmds.append(f'cd /www/wwwroot/{domain} && {framework_info["build_cmd"]}')
        if framework_info.get('migrate_cmd'):
            cmds.append(f'cd /www/wwwroot/{domain} && {framework_info["migrate_cmd"]}')
        
        cmds += [
            f'chown -R www:www /www/wwwroot/{domain}',
            f'chattr -i /www/wwwroot/{domain}/.user.ini 2>/dev/null || true',
        ]
        
        for cmd in cmds:
            stdin, stdout, stderr = ssh.exec_command(cmd)
            exit_code = stdout.channel.recv_exit_status()
            if exit_code != 0:
                err = stderr.read().decode().strip()[:500]
                if 'already exists' not in err.lower():
                    await asyncio.sleep(0)  # yield to event loop
        
        ssh.close()
        return f'✅ Deploy realizado em {domain}!'
    except Exception as e:
        return f'❌ Erro SSH: {str(e)[:200]}'

# ── /start ──
@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_dir = context.user_data['user_dir']
    cfg = load_user_config(user_dir)
    
    if not cfg:
        await update.message.reply_text(
            '🤖 *Bot aaPanel Deploy*\n\n'
            'Nenhuma configuração encontrada.\n'
            'Vamos configurar seu primeiro servidor?',
            parse_mode='Markdown'
        )
        return await ask_add_server(update, context)
    
    return await show_server_selection(update, context)

async def ask_add_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        '📌 *Nome do servidor* (ex: netcup):\n'
        'Digite um nome pra identificar seu servidor.',
        parse_mode='Markdown'
    )
    context.user_data['awaiting'] = 'server_name'
    return SELECT_SERVER

# ── Server Selection ──
async def show_server_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_dir = context.user_data['user_dir']
    servers = get_servers(user_dir)
    
    if not servers:
        return await ask_add_server(update, context)
    
    keyboard = [[InlineKeyboardButton(f'🖥 {s}', callback_data=f'server_{s}')] for s in servers]
    keyboard.append([InlineKeyboardButton('⚙️ Configurar servidores', callback_data='config_servers')])
    
    await update.effective_message.reply_text(
        '🖥 *Qual servidor?*',
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return SELECT_SERVER

# ── Main Menu ──
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    server = context.user_data.get('selected_server', '?')
    keyboard = [
        [InlineKeyboardButton('1️⃣ Adicionar novo site', callback_data='add_site')],
        [InlineKeyboardButton('2️⃣ Atualizar site', callback_data='update_site')],
        [InlineKeyboardButton('3️⃣ Remover site', callback_data='remove_site')],
        [InlineKeyboardButton('⚙️ Ajustar configurações', callback_data='adjust_config')],
        [InlineKeyboardButton('🔙 Trocar servidor', callback_data='change_server')],
    ]
    await update.effective_message.reply_text(
        f'📋 *Servidor:* {server}\n\nO que fazer?',
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return MAIN_MENU

# ════════════════════════════════════════
# ADD SITE FLOW
# ════════════════════════════════════════

async def add_site_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        '📌 *Domínio:*\n'
        'Digite o domínio do site (ex: meusite.com)',
        parse_mode='Markdown'
    )
    return ADD_DOMAIN

async def add_site_check_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    domain = update.message.text.strip().lower()
    context.user_data['domain'] = domain
    user_dir = context.user_data['user_dir']
    server = context.user_data.get('selected_server')
    server_cfg = get_server_cfg(user_dir, server)
    
    existing = check_domain_exists(server_cfg, domain)
    if existing:
        keyboard = [
            [InlineKeyboardButton('✅ Sim, atualizar', callback_data='goto_update')],
            [InlineKeyboardButton('❌ Não, outro domínio', callback_data='add_another_domain')],
        ]
        await update.message.reply_text(
            f'⚠️ *{domain}* já existe no aaPanel!\nQuer atualizar em vez de criar?',
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return ADD_DOMAIN
    
    # Git
    return await ask_git_source(update, context)

async def ask_git_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton('📦 Sim, tenho repositório', callback_data='git_ask_link')],
        [InlineKeyboardButton('⏭ Pular (deploy manual)', callback_data='git_skip')],
    ]
    await update.effective_message.reply_text(
        '📦 *Git:*\nTem um repositório GitHub pra esse projeto?',
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return ADD_GIT_SOURCE

async def git_ask_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        '📌 Link do repositório:\n'
        '(ex: `https://github.com/user/repo.git`)\n'
        'ou digite 0 pra pular.',
        parse_mode='Markdown'
    )
    return ADD_GIT_LINK

async def git_set_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == '0':
        context.user_data['git_url'] = None
    else:
        context.user_data['git_url'] = text
    return await ask_db(update, context)

async def git_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['git_url'] = None
    return await ask_db(update, context)

async def ask_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_dir = context.user_data['user_dir']
    domain = context.user_data['domain']
    
    # Try to detect DB deps
    cwd = os.getcwd()
    pkg_json = Path(cwd) / 'package.json'
    composer_json = Path(cwd) / 'composer.json'
    
    detected_dep = None
    if pkg_json.exists():
        try:
            deps = json.loads(pkg_json.read_text()).get('dependencies', {})
            if 'prisma' in deps: detected_dep = 'Prisma'
            elif 'typeorm' in deps: detected_dep = 'TypeORM'
            elif '@mikro-orm/core' in deps: detected_dep = 'MikroORM'
        except: pass
    elif composer_json.exists():
        try:
            requires = json.loads(composer_json.read_text()).get('require', {})
            if 'laravel/framework' in requires: detected_dep = 'Laravel'
            if 'doctrine/orm' in requires: detected_dep = 'Doctrine'
        except: pass
    
    if detected_dep:
        keyboard = [
            [InlineKeyboardButton(f'✅ Sim, criar BD pra {detected_dep}', callback_data='db_yes')],
            [InlineKeyboardButton('❌ Não', callback_data='db_no')],
        ]
        await update.effective_message.reply_text(
            f'🗄 Detectei *{detected_dep}* no projeto.\nCriar banco de dados?',
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    else:
        keyboard = [
            [InlineKeyboardButton('✅ Sim', callback_data='db_yes')],
            [InlineKeyboardButton('❌ Não', callback_data='db_no')],
        ]
        await update.effective_message.reply_text(
            '🗄 *Criar banco de dados?*',
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
    return ADD_DB_OPT_IN

async def db_choose_engine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton('MySQL', callback_data='engine_mysql')],
        [InlineKeyboardButton('PostgreSQL', callback_data='engine_pgsql')],
    ]
    await update.effective_message.reply_text(
        '🗄 *Engine do banco:*',
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return ADD_DB_ENGINE

async def db_set_engine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    domain = context.user_data['domain']
    name = domain.rsplit('.', 1)[0]  # meusite.com → meusite
    await update.effective_message.reply_text(
        f'📌 *Nome do BD:* (Enter = `{name}`)',
        parse_mode='Markdown'
    )
    return ADD_DB_NAME

async def db_set_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    domain = context.user_data['domain']
    default = domain.rsplit('.', 1)[0]
    context.user_data['db_name'] = text if text else default
    return await ask_ssl(update, context)

async def db_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['db_name'] = None
    context.user_data['db_engine'] = None
    return await ask_ssl(update, context)

async def ask_ssl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton('✅ Sim', callback_data='ssl_yes')],
        [InlineKeyboardButton('❌ Não', callback_data='ssl_no')],
    ]
    await update.effective_message.reply_text(
        '🔒 *Emitir SSL (Let\'s Encrypt)?*',
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return ADD_SSL

async def ask_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton('🚀 Sim, fazer deploy agora!', callback_data='deploy_yes')],
        [InlineKeyboardButton('⏸ Só configurar, depois faço', callback_data='deploy_no')],
    ]
    await update.effective_message.reply_text(
        '🚀 *Fazer deploy agora?*\n\n'
        'Vou clonar o repo no servidor, instalar dependências, buildar e rodar migrations.',
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return ADD_DEPLOY_CHOICE

async def execute_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_dir = context.user_data['user_dir']
    server = context.user_data.get('selected_server')
    server_cfg = get_server_cfg(user_dir, server)
    domain = context.user_data['domain']
    
    msg = await update.effective_message.reply_text('🔄 Executando deploy...')
    
    # 1. Create site on aaPanel
    await msg.edit_text(f'🔧 Criando site {domain} no aaPanel...')
    site_result = create_aapanel_site(server_cfg, domain)
    status = site_result.get('status', site_result.get('success'))
    if not status:
        site_ok = True  # might already exist
    else:
        site_ok = True
    
    # 2. Create database if needed
    db_name = context.user_data.get('db_name')
    db_engine = context.user_data.get('db_engine', 'mysql')
    if db_name:
        await msg.edit_text(f'🗄 Criando banco {db_name}...')
        db_result = create_database(server_cfg, db_name, db_engine)
    
    # 3. SSL if needed
    if context.user_data.get('ssl', False):
        await msg.edit_text(f'🔒 Emitindo SSL para {domain}...')
        ssl_result = set_ssl(server_cfg, domain)
    
    # 4. Git
    git_url = context.user_data.get('git_url')
    if git_url:
        # Detect framework from local project
        framework = detect_framework(os.getcwd())
        await msg.edit_text(f'📦 Clonando repo e deploy...')
        deploy_result = await deploy_via_ssh(server_cfg, domain, git_url, framework)
    else:
        deploy_result = '⏭ Deploy manual — repo não configurado.'
    
    # Summary
    summary = (
        f'══════════════════\n'
        f'   ✅ *Resumo*\n'
        f'══════════════════\n'
        f'   Servidor:  {server}\n'
        f'   Domínio:   {domain}\n'
        f'   BD:        {"✅ " + db_name if db_name else "❌"}\n'
        f'   SSL:       {"✅" if context.user_data.get("ssl") else "❌"}\n'
        f'   Deploy:    {deploy_result}\n'
        f'══════════════════'
    )
    await msg.edit_text(summary, parse_mode='Markdown')
    
    # Loop: add another?
    keyboard = [
        [InlineKeyboardButton('✅ Adicionar outro', callback_data='add_another')],
        [InlineKeyboardButton('🔙 Voltar ao menu', callback_data='back_menu')],
    ]
    await update.effective_message.reply_text(
        '❓ *Adicionar outro domínio?*',
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return ADD_RESULT

async def deploy_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_dir = context.user_data['user_dir']
    server = context.user_data.get('selected_server')
    domain = context.user_data['domain']
    
    # Just create site + DB, no deploy
    server_cfg = get_server_cfg(user_dir, server)
    create_aapanel_site(server_cfg, domain)
    db_name = context.user_data.get('db_name')
    if db_name:
        create_database(server_cfg, db_name, context.user_data.get('db_engine', 'mysql'))
    
    summary = (
        f'══════════════════\n'
        f'   ✅ *Configurado*\n'
        f'══════════════════\n'
        f'   Servidor:  {server}\n'
        f'   Domínio:   {domain}\n'
        f'   BD:        {"✅ " + db_name if db_name else "❌"}\n'
        f'   SSL:       {"✅" if context.user_data.get("ssl") else "❌"}\n'
        f'   Status:    ⏸ Configurado, deploy pendente\n'
        f'══════════════════'
    )
    await update.effective_message.reply_text(summary, parse_mode='Markdown')
    
    keyboard = [
        [InlineKeyboardButton('✅ Adicionar outro', callback_data='add_another')],
        [InlineKeyboardButton('🔙 Voltar ao menu', callback_data='back_menu')],
    ]
    await update.effective_message.reply_text(
        '❓ *Adicionar outro domínio?*',
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return ADD_RESULT

# ════════════════════════════════════════
# UPDATE SITE FLOW
# ════════════════════════════════════════

async def update_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        '📌 *Qual domínio atualizar?*',
        parse_mode='Markdown'
    )
    return UPDATE_DOMAIN

async def update_check_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    domain = update.message.text.strip().lower()
    user_dir = context.user_data['user_dir']
    server = context.user_data.get('selected_server')
    server_cfg = get_server_cfg(user_dir, server)
    
    existing = check_domain_exists(server_cfg, domain)
    if not existing:
        keyboard = [
            [InlineKeyboardButton('✅ Adicionar este domínio', callback_data='goto_add')],
            [InlineKeyboardButton('🔙 Voltar', callback_data='back_menu')],
        ]
        await update.message.reply_text(
            f'❌ *{domain}* não encontrado no servidor.\n'
            f'Quer adicionar em vez disso?',
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return UPDATE_DOMAIN
    
    context.user_data['update_domain'] = domain
    keyboard = [
        [InlineKeyboardButton('1️⃣ Código (git pull)', callback_data='up_code')],
        [InlineKeyboardButton('2️⃣ Dependências (install)', callback_data='up_deps')],
        [InlineKeyboardButton('3️⃣ Build + restart', callback_data='up_build')],
        [InlineKeyboardButton('4️⃣ SSL (renovar)', callback_data='up_ssl')],
        [InlineKeyboardButton('5️⃣ Trocar repositório git', callback_data='up_git')],
        [InlineKeyboardButton('6️⃣ Tudo', callback_data='up_all')],
        [InlineKeyboardButton('🔙 Voltar', callback_data='back_menu')],
    ]
    await update.message.reply_text(
        f'📋 *{domain}*\n\nO que atualizar?',
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return UPDATE_CHOOSE

async def execute_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.replace('up_', '')
    user_dir = context.user_data['user_dir']
    server = context.user_data.get('selected_server')
    server_cfg = get_server_cfg(user_dir, server)
    domain = context.user_data.get('update_domain')
    
    host = server_cfg.get('host')
    user = server_cfg.get('user', 'root')
    password = server_cfg.get('password')
    
    msg = await query.edit_message_text(f'🔄 Executando update em {domain}...')
    
    import paramiko
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host, username=user, password=password, timeout=10)
        
        cmds = []
        if action in ('code', 'all'):
            cmds.append(f'cd /www/wwwroot/{domain} && git pull')
        if action in ('deps', 'all'):
            cmds.append(f'cd /www/wwwroot/{domain} && (npm install || composer install || pip install -r requirements.txt)')
        if action in ('build', 'all'):
            cmds.append(f'cd /www/wwwroot/{domain} && (npm run build || echo "No build step")')
        if action in ('ssl',):
            api_key = server_cfg.get('aapanel', {}).get('api_key', '')
            base_url = server_cfg.get('aapanel', {}).get('url', '')
            result = set_ssl(server_cfg, domain)
        
        for cmd in cmds:
            stdin, stdout, stderr = ssh.exec_command(cmd)
            stdout.channel.recv_exit_status()
        
        ssh.close()
        await msg.edit_text(f'✅ *{domain}* atualizado com sucesso!', parse_mode='Markdown')
    except Exception as e:
        await msg.edit_text(f'❌ Erro: {str(e)[:200]}', parse_mode='Markdown')
    
    keyboard = [[InlineKeyboardButton('🔙 Voltar ao menu', callback_data='back_menu')]]
    await query.message.reply_text('❓ Voltar ao menu?', reply_markup=InlineKeyboardMarkup(keyboard))
    return UPDATE_EXEC

# ════════════════════════════════════════
# REMOVE SITE FLOW
# ════════════════════════════════════════

async def remove_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        '📌 *Qual domínio remover?*',
        parse_mode='Markdown'
    )
    return REMOVE_DOMAIN

async def remove_check_domain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    domain = update.message.text.strip().lower()
    user_dir = context.user_data['user_dir']
    server = context.user_data.get('selected_server')
    server_cfg = get_server_cfg(user_dir, server)
    
    existing = check_domain_exists(server_cfg, domain)
    if not existing:
        keyboard = [[InlineKeyboardButton('🔙 Voltar', callback_data='back_menu')]]
        await update.message.reply_text(
            f'❌ *{domain}* não encontrado.',
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )
        return REMOVE_DOMAIN
    
    context.user_data['remove_domain'] = domain
    context.user_data['remove_site_id'] = existing.get('id')
    
    keyboard = [
        [InlineKeyboardButton('⚠️ Sim, remover!', callback_data='confirm_remove')],
        [InlineKeyboardButton('❌ Cancelar', callback_data='back_menu')],
    ]
    await update.message.reply_text(
        f'⚠️ *Confirmar remoção de {domain}?*\n\n'
        f'Isso vai remover o site do aaPanel.\n'
        f'(BD e arquivos você decide depois)',
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return REMOVE_CONFIRM

async def execute_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_dir = context.user_data['user_dir']
    server = context.user_data.get('selected_server')
    server_cfg = get_server_cfg(user_dir, server)
    domain = context.user_data.get('remove_domain')
    site_id = context.user_data.get('remove_site_id')
    
    msg = await query.edit_message_text(f'🗑 Removendo {domain}...')
    
    # Remove from aaPanel
    api_key = server_cfg.get('aapanel', {}).get('api_key', '')
    base_url = server_cfg.get('aapanel', {}).get('url', '')
    result = aapanel_request(base_url, api_key, '/site', 'DeleteSite',
                             {'id': site_id, 'webname': json.dumps([domain])}, method='POST')
    
    await msg.edit_text(f'✅ Site *{domain}* removido do aaPanel.', parse_mode='Markdown')
    
    # Ask about DB and files (via buttons in follow-up - keep it simple for now)
    keyboard = [[InlineKeyboardButton('🔙 Voltar ao menu', callback_data='back_menu')]]
    await query.message.reply_text('❓ Voltar ao menu?', reply_markup=InlineKeyboardMarkup(keyboard))
    return REMOVE_CONFIRM  # state doesn't matter, callback will end

# ════════════════════════════════════════
# CONFIG FLOW
# ════════════════════════════════════════

async def config_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_dir = context.user_data['user_dir']
    cfg = load_user_config(user_dir)
    
    # Show current config
    config_text = '📋 *Configuração atual:*\n'
    if cfg.get('servidores'):
        config_text += '\n🏢 Servidores:\n'
        for name, srv in cfg['servidores'].items():
            config_text += f'   • {name} — `{srv.get("host", "?")}`\n'
    if cfg.get('git'):
        config_text += '\n🌐 Git:\n'
        for plat, accs in cfg.get('git', {}).items():
            for user in accs:
                config_text += f'   • {plat}:{user}\n'
    
    keyboard = [
        [InlineKeyboardButton('➕ Adicionar servidor', callback_data='cfg_add_server')],
        [InlineKeyboardButton('➖ Remover servidor', callback_data='cfg_rm_server')],
        [InlineKeyboardButton('➕ Adicionar conta git', callback_data='cfg_add_git')],
        [InlineKeyboardButton('➖ Remover conta git', callback_data='cfg_rm_git')],
        [InlineKeyboardButton('🔙 Voltar ao menu', callback_data='back_menu')],
    ]
    await update.effective_message.reply_text(
        config_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )
    return CONFIG_MENU

async def config_add_server_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        '📌 *Adicionar servidor*\n\n'
        'Me envie os dados assim:\n'
        '`nome, ip, user(root), senha, api_key, url_aapanel`\n\n'
        'Ou digite 0 pra cancelar.',
        parse_mode='Markdown'
    )
    context.user_data['awaiting'] = 'cfg_server'
    return CONFIG_MENU

async def config_handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == '0':
        return await config_menu(update, context)
    
    user_dir = context.user_data['user_dir']
    awaiting = context.user_data.get('awaiting')
    
    if awaiting == 'cfg_server':
        parts = [p.strip() for p in text.split(',')]
        if len(parts) >= 5:
            name, host = parts[0], parts[1]
            user = parts[2] if len(parts) > 2 else 'root'
            password = parts[3] if len(parts) > 3 else ''
            api_key = parts[4] if len(parts) > 4 else ''
            url = parts[5] if len(parts) > 5 else f'https://{host}'
            
            cfg = load_user_config(user_dir)
            if 'servidores' not in cfg:
                cfg['servidores'] = {}
            cfg['servidores'][name] = {
                'host': host, 'user': user, 'password': password,
                'aapanel': {'api_key': api_key, 'entrance': '', 'url': url}
            }
            save_user_config(user_dir, cfg)
            await update.message.reply_text(f'✅ Servidor *{name}* adicionado!', parse_mode='Markdown')
        else:
            await update.message.reply_text('❌ Formato inválido. Use: nome, ip, user, senha, api_key, url')
    
    elif awaiting == 'cfg_git':
        parts = [p.strip() for p in text.split(',')]
        if len(parts) >= 3:
            platform, user, token = parts[0], parts[1], parts[2]
            cfg = load_user_config(user_dir)
            if 'git' not in cfg:
                cfg['git'] = {}
            if platform not in cfg['git']:
                cfg['git'][platform] = {}
            cfg['git'][platform][user] = {'token': token, 'email': parts[3] if len(parts) > 3 else ''}
            save_user_config(user_dir, cfg)
            await update.message.reply_text(f'✅ Conta git *{platform}:{user}* adicionada!', parse_mode='Markdown')
        else:
            await update.message.reply_text('❌ Formato inválido. Use: plataforma, usuario, token')
    
    return await config_menu(update, context)

# ════════════════════════════════════════
# CALLBACK HANDLER (all button clicks)
# ════════════════════════════════════════

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    # ── Server selection ──
    if data.startswith('server_'):
        context.user_data['selected_server'] = data.replace('server_', '')
        await query.edit_message_text(f'✅ Servidor *{context.user_data["selected_server"]}* selecionado!', parse_mode='Markdown')
        return await show_main_menu(update, context)
    
    if data == 'config_servers':
        return await config_menu(update, context)
    
    # ── Main Menu ──
    if data == 'add_site':
        return await add_site_domain(update, context)
    if data == 'update_site':
        return await update_domain(update, context)
    if data == 'remove_site':
        return await remove_domain(update, context)
    if data == 'adjust_config':
        return await config_menu(update, context)
    if data == 'change_server':
        return await show_server_selection(update, context)
    if data == 'back_menu':
        return await show_main_menu(update, context)
    
    # ── Add Site flow ──
    if data == 'add_another_domain':
        return await add_site_domain(update, context)
    if data == 'goto_update':
        return await update_domain(update, context)
    if data == 'add_another':
        return await add_site_domain(update, context)
    
    # Git
    if data == 'git_ask_link':
        return await git_ask_link(update, context)
    if data == 'git_skip':
        return await git_skip(update, context)
    
    # DB
    if data == 'db_yes':
        return await db_choose_engine(update, context)
    if data == 'db_no':
        return await db_skip(update, context)
    if data.startswith('engine_'):
        context.user_data['db_engine'] = data.replace('engine_', '')
        return await db_set_engine(update, context)
    
    # SSL
    if data == 'ssl_yes':
        context.user_data['ssl'] = True
        return await ask_deploy(update, context)
    if data == 'ssl_no':
        context.user_data['ssl'] = False
        return await ask_deploy(update, context)
    
    # Deploy
    if data == 'deploy_yes':
        return await execute_deploy(update, context)
    if data == 'deploy_no':
        return await deploy_skip(update, context)
    
    # ── Update ──
    if data.startswith('up_'):
        return await execute_update(update, context)
    if data == 'goto_add':
        return await add_site_domain(update, context)
    
    # ── Remove ──
    if data == 'confirm_remove':
        return await execute_remove(update, context)
    
    # ── Config ──
    if data == 'cfg_add_server':
        return await config_add_server_prompt(update, context)
    if data == 'cfg_rm_server':
        user_dir = context.user_data['user_dir']
        servers = get_servers(user_dir)
        if not servers:
            await query.edit_message_text('❌ Nenhum servidor configurado.')
            return await config_menu(update, context)
        keyboard = [[InlineKeyboardButton(f'🗑 {s}', callback_data=f'del_server_{s}')] for s in servers]
        keyboard.append([InlineKeyboardButton('🔙 Cancelar', callback_data='adjust_config')])
        await query.edit_message_text('🗑 *Remover servidor:*',reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return CONFIG_MENU
    if data.startswith('del_server_'):
        name = data.replace('del_server_', '')
        user_dir = context.user_data['user_dir']
        cfg = load_user_config(user_dir)
        if name in cfg.get('servidores', {}):
            del cfg['servidores'][name]
            save_user_config(user_dir, cfg)
            await query.edit_message_text(f'✅ Servidor *{name}* removido!', parse_mode='Markdown')
        return await config_menu(update, context)
    
    if data == 'cfg_add_git':
        await query.edit_message_text(
            '📌 *Adicionar conta git*\n\n'
            'Me envie os dados:\n'
            '`plataforma, usuario, token`\n\n'
            'Ex: `github, lua, ghp_TOKEN`\n'
            'Ou digite 0 pra cancelar.',
            parse_mode='Markdown'
        )
        context.user_data['awaiting'] = 'cfg_git'
        return CONFIG_MENU
    if data == 'cfg_rm_git':
        user_dir = context.user_data['user_dir']
        accounts = get_git_accounts(user_dir)
        if not accounts:
            await query.edit_message_text('❌ Nenhuma conta git configurada.')
            return await config_menu(update, context)
        keyboard = [[InlineKeyboardButton(f'🗑 {a}', callback_data=f'del_git_{a}')] for a in accounts]
        keyboard.append([InlineKeyboardButton('🔙 Cancelar', callback_data='adjust_config')])
        await query.edit_message_text('🗑 *Remover conta git:*', reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        return CONFIG_MENU
    if data.startswith('del_git_'):
        account_str = data.replace('del_git_', '')
        user_dir = context.user_data['user_dir']
        parts = account_str.split(':', 1)
        if len(parts) == 2:
            plat, user = parts
            cfg = load_user_config(user_dir)
            if plat in cfg.get('git', {}) and user in cfg['git'][plat]:
                del cfg['git'][plat][user]
                if not cfg['git'][plat]:
                    del cfg['git'][plat]
                save_user_config(user_dir, cfg)
                await query.edit_message_text(f'✅ Conta *{account_str}* removida!', parse_mode='Markdown')
        return await config_menu(update, context)
    
    return await show_main_menu(update, context)

# ── Fallback for text input ──
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fallback for text input in conversation states"""
    state = context.user_data.get('awaiting')
    
    # Config editor commands
    if state in ('cfg_server', 'cfg_git'):
        return await config_handle_input(update, context)
    
    # ── First run flow: ask one field at a time ──
    text = update.message.text.strip()
    
    if state == 'server_name':
        context.user_data['new_server_name'] = text
        context.user_data['awaiting'] = 'server_ip'
        await update.message.reply_text(
            f'✅ Servidor *{text}*\n\n📌 *IP do servidor:*',
            parse_mode='Markdown'
        )
        return SELECT_SERVER
    
    if state == 'server_ip':
        context.user_data['new_server_ip'] = text
        context.user_data['awaiting'] = 'git_platform'
        await update.message.reply_text(
            '🌐 *Plataforma git:* (github / gitlab)',
            parse_mode='Markdown'
        )
        return SELECT_SERVER
    
    if state == 'git_platform':
        if text.lower() not in ('github', 'gitlab'):
            await update.message.reply_text('❌ Digite *github* ou *gitlab*', parse_mode='Markdown')
            return SELECT_SERVER
        context.user_data['new_git_platform'] = text.lower()
        context.user_data['awaiting'] = 'git_user'
        await update.message.reply_text(
            '🌐 *Usuário git:*',
            parse_mode='Markdown'
        )
        return SELECT_SERVER
    
    if state == 'git_user':
        context.user_data['new_git_user'] = text
        # Save config
        name = context.user_data.get('new_server_name', 'default')
        ip = context.user_data.get('new_server_ip', '')
        platform = context.user_data.get('new_git_platform', 'github')
        user = text
        
        user_dir = context.user_data['user_dir']
        cfg = {}
        cfg['servidores'] = {name: {'host': ip, 'user': 'root', 'password': '', 'aapanel': {'api_key': '', 'entrance': '', 'url': f'https://{ip}'}}}
        cfg['git'] = {platform: {user: {'token': '', 'email': ''}}}
        save_user_config(user_dir, cfg)
        
        # Cleanup
        for k in ['new_server_name', 'new_server_ip', 'new_git_platform', 'new_git_user', 'awaiting']:
            context.user_data.pop(k, None)
        
        context.user_data['selected_server'] = name
        await update.message.reply_text('✅ *Config salva!*', parse_mode='Markdown')
        return await show_main_menu(update, context)
    
    # Nothing matched — show hint
    await update.message.reply_text('Use os botões do menu :)')

# ── /restart command ──
async def restart_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('🔄 Reiniciando...')
    os.execl(sys.executable, sys.executable, *sys.argv)

# ── Cancel ──
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('👋 Até mais!')
    return ConversationHandler.END

# ════════════════════════════════════════
# MAIN
# ════════════════════════════════════════

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            SELECT_SERVER: [
                CallbackQueryHandler(button_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            ],
            MAIN_MENU: [CallbackQueryHandler(button_callback)],
            
            ADD_DOMAIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_site_check_domain),
                CallbackQueryHandler(button_callback),
            ],
            ADD_GIT_SOURCE: [CallbackQueryHandler(button_callback)],
            ADD_GIT_LINK: [MessageHandler(
                filters.TEXT & ~filters.COMMAND, git_set_link)],
            ADD_DB_OPT_IN: [CallbackQueryHandler(button_callback)],
            ADD_DB_ENGINE: [CallbackQueryHandler(button_callback)],
            ADD_DB_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, db_set_name)],
            ADD_SSL: [CallbackQueryHandler(button_callback)],
            ADD_DEPLOY_CHOICE: [CallbackQueryHandler(button_callback)],
            ADD_RESULT: [CallbackQueryHandler(button_callback)],
            
            UPDATE_DOMAIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, update_check_domain),
                CallbackQueryHandler(button_callback),
            ],
            UPDATE_CHOOSE: [CallbackQueryHandler(button_callback)],
            UPDATE_EXEC: [CallbackQueryHandler(button_callback)],
            
            REMOVE_DOMAIN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, remove_check_domain),
                CallbackQueryHandler(button_callback),
            ],
            REMOVE_CONFIRM: [CallbackQueryHandler(button_callback)],
            
            CONFIG_MENU: [
                CallbackQueryHandler(button_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text),
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        per_message=False,
    )
    
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler('restart', restart_bot, filters=filters.User(user_id=ALLOWED_USERS)))
    
    logger.info('🤖 Bot aapanel-deploy rodando...')
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
