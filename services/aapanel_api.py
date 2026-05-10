import hashlib
import time
import json
import requests

class AapanelAPI:
    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
    
    def _auth(self):
        """Generate auth token for aaPanel API"""
        t = str(int(time.time()))
        token = hashlib.md5((t + hashlib.md5(self.api_key.encode()).hexdigest()).encode()).hexdigest()
        return t, token
    
    def _request(self, endpoint, action, data=None, method='GET'):
        """Make request to aaPanel API"""
        t, token = self._auth()
        url = f'{self.base_url}{endpoint}'
        params = {'action': action, 't': t, 'token': token}
        if data:
            params.update(data)
        if method == 'GET':
            resp = requests.get(url, params=params, verify=False, timeout=15)
        else:
            resp = requests.post(url, data=params, verify=False, timeout=15)
        return resp.json()
    
    def list_sites(self):
        """List all sites"""
        return self._request('/data', 'getData', {'table': 'sites'})
    
    def get_site(self, domain):
        """Check if a domain exists, return site data or None"""
        result = self.list_sites()
        if isinstance(result, dict) and result.get('status') is True:
            sites = result.get('data', [])
            for site in sites:
                if site.get('name') == domain or domain in site.get('domain', ''):
                    return site
        return None
    
    def create_site(self, domain, path=None, type_id='0', site_type='static', port='80'):
        """Create a new site"""
        if path is None:
            path = f'/www/wwwroot/{domain}'
        data = {
            'domain': domain,
            'path': path,
            'type_id': type_id,
            'type': site_type,
            'port': port,
        }
        result = self._request('/site', 'AddSite', data, method='POST')
        return result
    
    def delete_site(self, site_id, domain):
        """Delete a site"""
        data = {
            'id': site_id,
            'webname': json.dumps([domain])
        }
        return self._request('/site', 'DeleteSite', data, method='POST')
    
    def set_ssl(self, domain):
        """Set Let's Encrypt SSL"""
        data = {'domain': domain, 'type': '1'}
        return self._request('/site', 'SetSSL', data, method='POST')
    
    def list_databases(self):
        """List all databases"""
        return self._request('/data', 'getData', {'table': 'databases'})
    
    def create_database(self, name, db_type='mysql', encoding=None):
        """Create a new database"""
        if encoding is None:
            encoding = 'utf8mb4' if db_type == 'mysql' else 'UTF8'
        data = {'name': name, 'codeing': encoding, 'type': db_type}
        return self._request('/database', 'AddDatabase', data, method='POST')
    
    def delete_database(self, db_id, db_name):
        """Delete a database"""
        data = {'id': db_id, 'name': db_name}
        return self._request('/database', 'DeleteDatabase', data, method='POST')
    
    def get_php_versions(self):
        """List available PHP versions"""
        return self._request('/site', 'GetPHPVersion')
