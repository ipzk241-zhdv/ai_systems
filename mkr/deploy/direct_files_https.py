"""Direct IP HTTPS for the existing Oracle file server, with automatic renewal."""
import argparse
import ipaddress
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile


SCRIPT = Path('/usr/local/lib/news-bot/direct_files_https.py')
ENV = Path('/home/ubuntu/ai_systems/mkr/solution/.env')
WEBROOT = Path('/var/www/news-files-acme')
CERTBOT = Path('/usr/local/bin/news-files-certbot')
SITE = Path('/etc/nginx/sites-available/news-files')


def run(args, label):
    print(label + '…', flush=True)
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode:
        raise RuntimeError(label + ' failed:\n' + result.stdout[-3000:])
    return result.stdout


def write(path, text, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    path.chmod(mode)


def config():
    values = {}
    for line in ENV.read_text(encoding='utf-8-sig').splitlines():
        key, separator, value = line.partition('=')
        if separator and key.strip() in {'FILES_PORT','FILES_MAX_FILE_MB','FILES_ENABLED'}:
            values[key.strip()] = value.strip().strip('\"\'')
    if values.get('FILES_ENABLED') != '1':
        raise ValueError('The file manager must be enabled')
    port, maximum = int(values.get('FILES_PORT','8080')), int(values.get('FILES_MAX_FILE_MB','2048'))
    if not 1024 <= port <= 65535 or not 1 <= maximum <= 16384:
        raise ValueError('Invalid file server port or size limit')
    return port, maximum


def nginx_site(address, port, maximum, tls=False):
    ipaddress.ip_address(address)
    http = f'''server {{
    listen 80;
    server_name {address};
    location ^~ /.well-known/acme-challenge/ {{
        root {WEBROOT};
        default_type text/plain;
        try_files $uri =404;
    }}
    location / {{ return 301 https://{address}$request_uri; }}
}}
'''
    if not tls:
        return http
    return http + f'''server {{
    listen 443 ssl;
    server_name {address};
    ssl_certificate /etc/letsencrypt/live/news-files-ip/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/news-files-ip/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_tickets off;
    client_max_body_size {maximum}m;
    client_body_timeout 60s;
    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }}
}}
'''


def firewall():
    for port in ('80','443'):
        rule = ['INPUT','-p','tcp','--dport',port,'-j','ACCEPT']
        exists = subprocess.run(['iptables','-C',*rule], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if exists.returncode:
            subprocess.run(['iptables','-I',*rule], check=True)


def update_url(address):
    ipaddress.IPv4Address(address)
    original = ENV.read_bytes()
    status = ENV.stat()
    newline = '\r\n' if b'\r\n' in original else '\n'
    lines, found = [], False
    for line in original.decode('utf-8-sig').splitlines(keepends=True):
        if line.partition('=')[0].strip() == 'FILES_PUBLIC_URL':
            if not found:
                lines.append('FILES_PUBLIC_URL=https://' + address + newline)
                found=True
        else:
            lines.append(line)
    if not found:
        if lines and not lines[-1].endswith(('\n','\r')):
            lines[-1] += newline
        lines.append('FILES_PUBLIC_URL=https://' + address + newline)
    data=''.join(lines).encode('utf-8')
    if original.startswith(b'\xef\xbb\xbf'):
        data=b'\xef\xbb\xbf'+data
    descriptor, temporary = tempfile.mkstemp(prefix='.https-url-',dir=str(ENV.parent))
    try:
        if hasattr(os,'fchown'):
            os.fchown(descriptor,status.st_uid,status.st_gid)
        if hasattr(os,'fchmod'):
            os.fchmod(descriptor,stat.S_IMODE(status.st_mode) & 0o600)
        with os.fdopen(descriptor,'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary,str(ENV))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(address):
    port, maximum = config()
    run(['apt-get','install','-y','nginx'],'Installing Ubuntu Nginx')
    run([sys.executable,'-m','pip','install','--break-system-packages','--target','/opt/news-files-certbot',
         'certbot>=5.4,<6'],'Installing isolated Certbot dependencies')
    write(CERTBOT,"#!/usr/bin/python3\nimport sys\nsys.path.insert(0,'/opt/news-files-certbot')\nfrom certbot.main import main\nraise SystemExit(main())\n",0o755)
    SCRIPT.parent.mkdir(parents=True,exist_ok=True)
    if Path(__file__).resolve() != SCRIPT:
        shutil.copyfile(str(Path(__file__).resolve()),str(SCRIPT))
    SCRIPT.chmod(0o755)
    write('/etc/systemd/system/news-files-firewall.service',f'''[Unit]
Description=Allow HTTP and HTTPS for the news file manager
After=netfilter-persistent.service
Before=nginx.service
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {SCRIPT} --firewall-only
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
''')
    write('/etc/systemd/system/nginx.service.d/news-files.conf','[Unit]\nRequires=news-files-firewall.service\nAfter=news-files-firewall.service\n')
    write(SITE,nginx_site(address,port,maximum,tls=Path('/etc/letsencrypt/live/news-files-ip/fullchain.pem').exists()))
    enabled=Path('/etc/nginx/sites-enabled/news-files')
    if not enabled.exists():
        enabled.symlink_to(SITE)
    default=Path('/etc/nginx/sites-enabled/default')
    if default.is_symlink() and default.resolve()==Path('/etc/nginx/sites-available/default'):
        default.unlink()
    probe=WEBROOT/'.well-known/acme-challenge/ready.txt'
    write(probe,'news-files-acme-ready\n')
    run(['nginx','-t'],'Checking Nginx configuration')
    run(['systemctl','daemon-reload'],'Reloading service definitions')
    run(['systemctl','enable','--now','news-files-firewall.service'],'Enabling HTTP/HTTPS firewall rules')
    run(['systemctl','enable','--now','nginx'],'Enabling Nginx')
    run(['systemctl','reload','nginx'],'Loading HTTP challenge configuration')
    print('Ready for certificate validation on public port 80.',flush=True)


def activate(address):
    port,maximum=config()
    common=[str(CERTBOT),'certonly','--non-interactive','--agree-tos','--register-unsafely-without-email',
            '--webroot','--webroot-path',str(WEBROOT),'--preferred-profile','shortlived','--ip-address',address]
    run(common+['--staging','--cert-name','news-files-ip-staging'],'Validating IP with the staging CA')
    run(common+['--cert-name','news-files-ip'],'Obtaining trusted IP certificate')
    write(SITE,nginx_site(address,port,maximum,tls=True))
    run(['nginx','-t'],'Checking HTTPS configuration')
    run(['systemctl','reload','nginx'],'Enabling trusted HTTPS')
    write('/etc/systemd/system/news-files-cert-renew.service',f'''[Unit]
Description=Renew the file manager IP certificate
[Service]
Type=oneshot
ExecStart={CERTBOT} renew --cert-name news-files-ip --quiet --deploy-hook "/usr/bin/systemctl reload nginx"
''')
    write('/etc/systemd/system/news-files-cert-renew.timer','''[Unit]
Description=Check file manager certificate renewal every six hours
[Timer]
OnCalendar=*-*-* 00,06,12,18:00:00
RandomizedDelaySec=300
Persistent=true
[Install]
WantedBy=timers.target
''')
    run(['systemctl','daemon-reload'],'Loading certificate renewal timer')
    run(['systemctl','enable','--now','news-files-cert-renew.timer'],'Enabling certificate renewal')
    # Stop the old URL writer before changing the origin to the direct address.
    run(['systemctl','disable','--now','news-files-tunnel.service'],'Stopping the unused Cloudflare tunnel')
    update_url(address)
    print('Direct file manager HTTPS URL: https://'+address,flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--ip',default='141.147.21.157')
    parser.add_argument('--prepare',action='store_true')
    parser.add_argument('--activate',action='store_true')
    parser.add_argument('--firewall-only',action='store_true')
    parser.add_argument('--refresh-proxy',action='store_true')
    args=parser.parse_args()
    ipaddress.IPv4Address(args.ip)
    if os.geteuid()!=0:
        raise SystemExit('Run this infrastructure setup with sudo.')
    if args.firewall_only:
        firewall()
    elif args.prepare:
        prepare(args.ip)
    elif args.activate:
        activate(args.ip)
    elif args.refresh_proxy:
        port,maximum=config()
        write(SITE,nginx_site(args.ip,port,maximum,tls=True))
        run(['nginx','-t'],'Checking proxy configuration')
        run(['systemctl','reload','nginx'],'Reloading proxy limits')
    else:
        parser.error('Choose --prepare, --activate or --firewall-only')


if __name__=='__main__':
    main()
