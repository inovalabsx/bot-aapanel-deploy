import os
import sys
import yaml
import json
from pathlib import Path

CONFIG_DIR = Path.home() / '.aapanel-deploy'
CONFIG_PATH = CONFIG_DIR / 'config.yaml'

def load():
    """Load config file"""
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}

def save(cfg):
    """Save config file"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, 'w') as f:
        yaml.dump(cfg, f, default_flow_style=False)

def is_configured():
    """Check if config has at least one server"""
    cfg = load()
    return bool(cfg.get('servidores')) or bool(cfg.get('git'))

def format_config(cfg):
    """Format config for display (mask tokens)"""
    lines = []
    if cfg.get('servidores'):
        lines.append('🏢 **Servidores:**')
        for name, srv in cfg['servidores'].items():
            host = srv.get('host', '?')
            lines.append(f'   • **{name}** — `{host}`')
    if cfg.get('git'):
        lines.append('🌐 **Git:**')
        for plat, accounts in cfg['git'].items():
            for user in accounts:
                lines.append(f'   • **{plat}** — `{user}`')
    if not lines:
        return '📋 Nenhuma configuração encontrada.'
    return '\n'.join(lines)

def get_servers():
    """Get list of server names"""
    cfg = load()
    return list(cfg.get('servidores', {}).keys())

def get_server(name):
    """Get server config by name"""
    cfg = load()
    return cfg.get('servidores', {}).get(name)

def get_git_accounts():
    """Get list of git accounts as 'platform:user' strings"""
    cfg = load()
    accounts = []
    for plat, accs in cfg.get('git', {}).items():
        for user in accs:
            accounts.append(f'{plat}:{user}')
    return accounts

def get_git_token(account_str):
    """Get token for a 'platform:user' account string"""
    cfg = load()
    parts = account_str.split(':', 1)
    if len(parts) != 2:
        return None
    plat, user = parts
    return cfg.get('git', {}).get(plat, {}).get(user, {}).get('token')

def add_server(name, host, password='', user='root', api_key='', entrance='', url=''):
    """Add or update a server"""
    cfg = load()
    if 'servidores' not in cfg:
        cfg['servidores'] = {}
    cfg['servidores'][name] = {
        'host': host,
        'user': user,
        'password': password,
        'aapanel': {
            'api_key': api_key,
            'entrance': entrance,
            'url': url,
        }
    }
    save(cfg)

def remove_server(name):
    """Remove a server"""
    cfg = load()
    if name in cfg.get('servidores', {}):
        del cfg['servidores'][name]
        save(cfg)
        return True
    return False

def add_git_account(platform, user, token, email=''):
    """Add or update a git account"""
    cfg = load()
    if 'git' not in cfg:
        cfg['git'] = {}
    if platform not in cfg['git']:
        cfg['git'][platform] = {}
    cfg['git'][platform][user] = {'token': token}
    if email:
        cfg['git'][platform][user]['email'] = email
    save(cfg)

def remove_git_account(account_str):
    """Remove a git account by 'platform:user' string"""
    cfg = load()
    parts = account_str.split(':', 1)
    if len(parts) != 2:
        return False
    plat, user = parts
    if plat in cfg.get('git', {}) and user in cfg['git'][plat]:
        del cfg['git'][plat][user]
        if not cfg['git'][plat]:
            del cfg['git'][plat]
        save(cfg)
        return True
    return False
