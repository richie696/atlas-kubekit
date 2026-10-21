#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Interactive, bilingual manager for an existing Atlas KubeKit LB pair."""
from __future__ import annotations
import argparse
import base64
import importlib.util
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("atlas_i18n", ROOT / "scripts/lib/i18n.py")
i18n = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = i18n
assert spec.loader
spec.loader.exec_module(i18n)

REMOTE = r'''import csv, datetime, io, json, os, re, shutil, socket, subprocess, sys, tempfile, time
p=json.loads(sys.stdin.readline())
def run(args, check=True):
 r=subprocess.run(args,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
 if check and r.returncode: raise RuntimeError(r.stdout.strip() or 'command failed: '+str(args))
 return r.stdout.strip(),r.returncode
base='/etc/atlas-kubekit'; state_file=base+'/lb-manager.json'; hap='/etc/haproxy/haproxy.cfg'; keep='/etc/keepalived/keepalived.conf'
def state_read():
 try: return json.load(open(state_file))
 except FileNotFoundError: return {'vips':[],'routes':[]}
def status():
 out={}
 for svc in ('haproxy','keepalived'):
  out[svc]=run(['systemctl','is-active',svc],False)[0] or 'unknown'
 out['hostname']=run(['hostname','-s'],False)[0]
 out['addresses']=run(['bash','-lc','ip -o -4 addr show | awk \'{print $2" "$4}\''],False)[0]
 out['haproxy_config']=run(['haproxy','-c','-f',hap],False)[1]==0
 out['keepalived_config']=run(['keepalived','-t','-f',keep],False)[1]==0
 out['stats']=[]
 try:
  conn=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); conn.settimeout(2); conn.connect('/run/haproxy/admin.sock')
  conn.sendall(b'show stat\n'); chunks=[]
  while True:
   part=conn.recv(65536)
   if not part: break
   chunks.append(part)
  conn.close(); rows=csv.reader(io.StringIO(b''.join(chunks).decode(errors='replace')))
  header=next(rows,[])
  if header: header[0]=header[0].lstrip('# ')
  for values in rows:
   row=dict(zip(header,values))
   if row.get('svname') in ('FRONTEND','BACKEND'):
    out['stats'].append({k:row.get(k,'') for k in ('pxname','svname','status','scur','stot')})
 except Exception: pass
 out['state']=state_read()
 print(json.dumps(out))
def span(text, start):
 op=text.find('{',start)
 if op<0: raise RuntimeError('configuration block opening brace not found')
 depth=0; quote=None; esc=False
 for i in range(op,len(text)):
  c=text[i]
  if quote:
   if esc: esc=False
   elif c=='\\': esc=True
   elif c==quote: quote=None
   continue
  if c in ('"',"'"): quote=c
  elif c=='{': depth+=1
  elif c=='}':
   depth-=1
   if depth==0: return op,i
 raise RuntimeError('configuration block is incomplete')
def replace_managed(text, begin, end, body, before=None):
 marked=re.compile(re.escape(begin)+r'.*?'+re.escape(end),re.S)
 block=begin+'\n'+body.rstrip()+'\n'+end
 if marked.search(text): return marked.sub(lambda _m:block,text,count=1)
 if before is None: return text.rstrip()+'\n\n'+block+'\n'
 return text[:before].rstrip()+('\n' if text[:before].strip() else '')+block+'\n'+text[before:]
def create_candidates(data, token):
 with open(hap) as f: old_hap=f.read()
 with open(keep) as f: old_keep=f.read()
 st=data['state']; vips=st['vips']; routes=st['routes']
 vip_lines='\n'.join('        '+ip for ip in vips)
 start=old_keep.find('vrrp_instance VI_KUBE_APISERVER')
 if start<0: raise RuntimeError('VI_KUBE_APISERVER not found; refusing to replace Keepalived configuration')
 op,cl=span(old_keep,start); instance=old_keep[op+1:cl]
 vi=instance.find('virtual_ipaddress')
 if vi<0: raise RuntimeError('virtual_ipaddress block not found')
 vop,vcl=span(instance,vi)
 vblock=instance[vop+1:vcl]
 begin='# BEGIN ATLAS KUBEKIT MANAGED VIPS'; end='# END ATLAS KUBEKIT MANAGED VIPS'
 old_state=state_read(); previous=set(old_state.get('vips',[]))
 reserved=set(re.findall(r'(?m)^\s*([0-9]{1,3}(?:\.[0-9]{1,3}){3})(?=/|\s|$)',re.sub(re.escape(begin)+r'.*?'+re.escape(end),'',old_keep,flags=re.S)))
 local_ips=set(re.findall(r'\binet\s+([0-9]{1,3}(?:\.[0-9]{1,3}){3})/',run(['ip','-o','-4','addr','show'],False)[0]))
 conflicts=(set(vips)-previous) & (reserved | local_ips)
 if conflicts: raise RuntimeError('requested VIP already appears in Keepalived or on this node: '+', '.join(sorted(conflicts)))
 vblock=replace_managed(vblock,begin,end,vip_lines,before=len(vblock.rstrip()))
 instance=instance[:vop+1]+vblock+instance[vcl:]
 new_keep=old_keep[:op+1]+instance+old_keep[cl:]
 lines=[]
 for r in routes:
  n=r['name']; fe='ak_'+n; be='ak_'+n+'_backend'; mode=r['mode']; port=r['listenPort']; bind=r['bind']
  lines += ['', '# Atlas KubeKit route: '+n, 'frontend '+fe, '    bind '+bind+':'+str(port), '    mode '+mode]
  if mode=='http': lines += ['    option httplog']
  else: lines += ['    option tcplog']
  lines += ['    default_backend '+be, '', 'backend '+be, '    mode '+mode, '    balance roundrobin']
  if r.get('websocket'): lines += ['    timeout tunnel 1h']
  if mode=='http': lines += ['    option forwardfor']
  for ix,b in enumerate(r['backends'],1):
   lines += ['    server '+n+'_s'+str(ix)+' '+b+' check']
 body='\n'.join(lines).strip()
 new_hap=replace_managed(old_hap,'# BEGIN ATLAS KUBEKIT MANAGED ROUTES','# END ATLAS KUBEKIT MANAGED ROUTES',body)
 token_dir='/run/atlas-kubekit-lb-manager'; os.makedirs(token_dir,mode=0o700,exist_ok=True)
 d=token_dir+'/'+token; os.makedirs(d,mode=0o700,exist_ok=True)
 for name,content in (('haproxy.new',new_hap),('keepalived.new',new_keep),('haproxy.old',old_hap),('keepalived.old',old_keep)):
  with open(d+'/'+name,'w') as f: f.write(content)
  os.chmod(d+'/'+name,0o600)
 run(['haproxy','-c','-f',d+'/haproxy.new'])
 run(['keepalived','-t','-f',d+'/keepalived.new'])
 with open(d+'/state.new','w') as f: json.dump(st,f)
 with open(d+'/state.old','w') as f: json.dump(state_read(),f)
 if os.path.exists(state_file): open(d+'/state.existed','w').close()
 os.chmod(d+'/state.new',0o600); os.chmod(d+'/state.old',0o600)
 print(json.dumps({'prepared':True}))
def commit(data):
 token=data['token']; d='/run/atlas-kubekit-lb-manager/'+token
 if not os.path.isdir(d): raise RuntimeError('prepared transaction not found')
 stamp=datetime.datetime.now().strftime('%Y%m%d%H%M%S%f'); backup='/var/backups/atlas-kubekit/lb-manager/'+stamp
 os.makedirs(backup,mode=0o700,exist_ok=True); os.makedirs(base,mode=0o755,exist_ok=True)
 for name,path in (('haproxy.cfg',hap),('keepalived.conf',keep)):
  shutil.copy2(path,backup+'/'+name)
 shutil.copy2(state_file,backup+'/lb-manager.json') if os.path.exists(state_file) else None
 try:
  open(d+'/committed','w').close()
  shutil.copy2(d+'/haproxy.new',hap); shutil.copy2(d+'/keepalived.new',keep)
  shutil.copy2(d+'/state.new',state_file); os.chmod(state_file,0o600)
  run(['systemctl','reload','haproxy'])
  run(['systemctl','reload','keepalived'])
  run(['systemctl','is-active','haproxy']); run(['systemctl','is-active','keepalived'])
 except Exception:
  shutil.copy2(backup+'/haproxy.cfg',hap); shutil.copy2(backup+'/keepalived.conf',keep)
  if os.path.exists(backup+'/lb-manager.json'): shutil.copy2(backup+'/lb-manager.json',state_file)
  else:
   try: os.unlink(state_file)
   except FileNotFoundError: pass
  run(['systemctl','restart','haproxy'],False); run(['systemctl','reload','keepalived'],False)
  try: os.unlink(d+'/committed')
  except FileNotFoundError: pass
  raise
 print(json.dumps({'committed':True,'backup':backup}))
def rollback(data):
 token=data['token']; d='/run/atlas-kubekit-lb-manager/'+token
 if os.path.isdir(d):
  if os.path.exists(d+'/committed'):
   shutil.copy2(d+'/haproxy.old',hap); shutil.copy2(d+'/keepalived.old',keep)
   if os.path.exists(d+'/state.existed'): shutil.copy2(d+'/state.old',state_file)
   else:
    try: os.unlink(state_file)
    except FileNotFoundError: pass
   run(['systemctl','restart','haproxy'],False); run(['systemctl','reload','keepalived'],False)
  shutil.rmtree(d,ignore_errors=True)
 print(json.dumps({'rolledBack':True}))
def finish(data):
 shutil.rmtree('/run/atlas-kubekit-lb-manager/'+data['token'],ignore_errors=True); print(json.dumps({'finished':True}))
try:
 if p['action']=='status': status()
 elif p['action']=='prepare': create_candidates(p,p['token'])
 elif p['action']=='commit': commit(p)
 elif p['action']=='rollback': rollback(p)
 elif p['action']=='finish': finish(p)
 else: raise RuntimeError('unknown action')
except Exception as e:
 print(json.dumps({'error':str(e)})); sys.exit(1)
'''

class App:
    def __init__(self, lang: str, refresh: int):
        self.lang = lang
        self.refresh = refresh
        self.path = Path.home() / ".config/atlas-kubekit/lb-manager.json"
        self.config = self.load()

    def t(self, key: str, *args) -> str:
        return i18n.translate(self.lang, key, *args)

    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            data = {"sshUser": "ubuntu", "managed": [], "external": []}
        data.setdefault("sshUser", "ubuntu"); data.setdefault("managed", []); data.setdefault("external", [])
        return data

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.config, indent=2) + "\n")
        os.chmod(tmp, 0o600); tmp.replace(self.path)

    def ask(self, key, default=""):
        suffix = f" [{default}]" if default else ""
        value = input(self.t(key) + suffix + ": ").strip()
        return value or default

    def ssh(self, node, payload):
        encoded = base64.b64encode(REMOTE.encode()).decode()
        remote_code = f"import base64;exec(base64.b64decode('{encoded}'))"
        remote_command = "sudo -n python3 -c " + shlex.quote(remote_code)
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5",
                   f"{self.config['sshUser']}@{node['ip']}", remote_command]
        result = subprocess.run(command, input=json.dumps(payload) + "\n", text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(f"{node['name']} ({node['ip']}): {result.stdout.strip() or 'SSH/sudo failed'}")
        try: return json.loads(result.stdout.splitlines()[-1])
        except Exception: raise RuntimeError(result.stdout.strip() or "Invalid response from remote node")

    def nodes(self): return self.config["managed"]

    def ensure_nodes(self):
        if len(self.nodes()) < 2: raise RuntimeError(self.t("lb.error.need_pair"))
        for node in self.nodes():
            status=self.ssh(node, {"action":"status"})
            if status.get("haproxy") != "active" or status.get("keepalived") != "active":
                raise RuntimeError(self.t("lb.error.services",node["name"],status.get("haproxy"),status.get("keepalived")))
            if not status.get("haproxy_config") or not status.get("keepalived_config"):
                raise RuntimeError(self.t("lb.error.config",node["name"]))

    def get_states(self):
        states=[]
        for node in self.nodes(): states.append((node,self.ssh(node,{"action":"status"})))
        return states

    def dashboard(self):
        if not self.nodes(): print(self.t("lb.nodes.empty")); input(self.t("lb.press_enter")); return
        try:
            while True:
                states=self.get_states()
                os.system("clear")
                print(self.t("lb.dashboard.title", time.strftime("%Y-%m-%d %H:%M:%S")))
                print("-"*78)
                for node,s in states:
                    vip_owned=any(ipaddress.ip_address(vip) in [ipaddress.ip_interface(x.split()[-1]).ip for x in s.get('addresses','').splitlines() if '/' in x.split()[-1]] for vip in s.get('state',{}).get('vips',[]))
                    print(self.t("lb.dashboard.node", node['name'],node['ip']))
                    print(self.t("lb.dashboard.addresses",s.get('addresses','')))
                    print(self.t("lb.dashboard.services",s.get('haproxy'),s.get('keepalived'),self.t("lb.dashboard.vip_owner") if vip_owned else ""))
                    print(self.t("lb.dashboard.validation",self.t("common.status.ok") if s.get('haproxy_config') else self.t("common.status.fail"),self.t("common.status.ok") if s.get('keepalived_config') else self.t("common.status.fail")))
                    stats=s.get('stats',[])
                    if stats:
                        frontends=[r for r in stats if r.get('svname')=='FRONTEND']; backends=[r for r in stats if r.get('svname')=='BACKEND']
                        current=sum(int(r.get('scur') or 0) for r in frontends); total=sum(int(r.get('stot') or 0) for r in frontends)
                        up=sum(1 for r in backends if r.get('status','').startswith('UP'))
                        print(self.t("lb.dashboard.stats",len(frontends),up,len(backends),current,total))
                    else: print(self.t("lb.dashboard.stats_unavailable"))
                print("-"*78)
                print(self.t("lb.dashboard.routes",len(states[0][1].get('state',{}).get('routes',[])),len(states[0][1].get('state',{}).get('vips',[]))))
                print(self.t("lb.dashboard.quit",self.refresh))
                time.sleep(self.refresh)
        except KeyboardInterrupt: pass
        except Exception as e: print(self.t("lb.error.operation",str(e))); input(self.t("lb.press_enter"))

    def change_state(self, mutate):
        self.ensure_nodes()
        snapshots=self.get_states()
        states=[s.get('state',{'vips':[],'routes':[]}) for _,s in snapshots]
        canonical=json.dumps(states[0],sort_keys=True)
        if any(json.dumps(s,sort_keys=True)!=canonical for s in states[1:]):
            raise RuntimeError(self.t("lb.error.state_mismatch"))
        desired=json.loads(canonical); mutate(desired)
        print(self.t("lb.warning.test"))
        print(self.t("lb.warning.route_scope"))
        if input(self.t("lb.change.confirm") + " [no]: ").strip().lower() not in ("yes", "y", "是"):
            print(self.t("lb.change.cancelled")); return
        token=f"{os.getpid()}-{int(time.time())}"
        prepared=[]; committed=[]
        try:
            for node,_ in snapshots:
                self.ssh(node,{"action":"prepare","state":desired,"token":token}); prepared.append(node)
            for node in prepared:
                self.ssh(node,{"action":"commit","token":token}); committed.append(node)
        except Exception:
            for node in reversed(prepared):
                try: self.ssh(node,{"action":"rollback","token":token})
                except Exception: pass
            raise
        for node in prepared:
            try: self.ssh(node,{"action":"finish","token":token})
            except Exception: pass
        print(self.t("lb.change.success",len(committed)))

    def valid_name(self, value):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,20}",value): raise ValueError(self.t("lb.error.name"))
        return value

    def vips_menu(self):
        while True:
            print("\n"+self.t("lb.vip.title")); print("1) "+self.t("lb.vip.add")); print("2) "+self.t("lb.vip.remove")); print("3) "+self.t("lb.vip.list")); print("0) "+self.t("common.menu.back"))
            choice=input(self.t("common.menu.choice")+": ").strip()
            try:
                if choice=="1":
                    vip=self.ipv4(self.ask("lb.vip.prompt"))
                    if vip=="10.20.1.9": raise ValueError(self.t("lb.error.protected_vip"))
                    def add(s):
                        if vip in s["vips"]: raise ValueError(self.t("lb.error.exists"))
                        s["vips"].append(vip); s["vips"].sort(key=lambda x:int(ipaddress.IPv4Address(x)))
                    self.change_state(add)
                elif choice=="2":
                    state=self.get_states()[0][1].get('state',{}); print(", ".join(state.get('vips',[])) or self.t("lb.list.empty"))
                    vip=self.ask("lb.vip.remove_prompt")
                    def rem(s):
                        if vip not in s["vips"]: raise ValueError(self.t("lb.error.not_found"))
                        s["vips"].remove(vip)
                    self.change_state(rem)
                elif choice=="3":
                    state=self.get_states()[0][1].get('state',{}); print("\n".join(state.get('vips',[])) or self.t("lb.list.empty")); input(self.t("lb.press_enter"))
                elif choice=="0": return
            except (KeyboardInterrupt,EOFError): return
            except Exception as e: print(self.t("lb.error.operation",str(e)))

    def route_menu(self):
        while True:
            print("\n"+self.t("lb.route.title")); print("1) "+self.t("lb.route.add")); print("2) "+self.t("lb.route.remove")); print("3) "+self.t("lb.route.list")); print("0) "+self.t("common.menu.back"))
            choice=input(self.t("common.menu.choice")+": ").strip()
            try:
                if choice=="1":
                    name=self.valid_name(self.ask("lb.route.name")); mode=self.ask("lb.route.mode","http").lower()
                    if mode not in ("http","tcp"): raise ValueError(self.t("lb.error.mode"))
                    raw_port=self.ask("lb.route.port")
                    try: port=int(raw_port)
                    except ValueError: raise ValueError(self.t("lb.error.port")) from None
                    if not 1<=port<=65535: raise ValueError(self.t("lb.error.port"))
                    # Bind on all local addresses so the standby LB can accept a VIP after failover.
                    vip="*"
                    raw=self.ask("lb.route.backends")
                    backends=[]
                    for item in raw.split(","):
                        addr,sep,p=item.strip().rpartition(":")
                        if not sep: raise ValueError(self.t("lb.error.backend_format"))
                        addr=self.ipv4(addr)
                        try: p=int(p)
                        except ValueError: raise ValueError(self.t("lb.error.port")) from None
                        if not 1<=p<=65535: raise ValueError(self.t("lb.error.port"))
                        backends.append(f"{addr}:{p}")
                    if not backends: raise ValueError(self.t("lb.error.backends"))
                    ws=mode=="http" and self.ask("lb.route.websocket","no").lower() in ("yes","y","是")
                    def add(s):
                        if any(r['name']==name for r in s['routes']): raise ValueError(self.t("lb.error.exists"))
                        if any(r['listenPort']==port and r['bind']==vip for r in s['routes']): raise ValueError(self.t("lb.error.port_conflict"))
                        s['routes'].append({'name':name,'mode':mode,'listenPort':port,'bind':vip,'backends':backends,'websocket':ws})
                    self.change_state(add)
                elif choice=="2":
                    state=self.get_states()[0][1].get('state',{}); print("\n".join(r['name'] for r in state.get('routes',[])) or self.t("lb.list.empty"))
                    name=self.ask("lb.route.remove_prompt")
                    def rem(s):
                        rows=[r for r in s['routes'] if r['name']==name]
                        if not rows: raise ValueError(self.t("lb.error.not_found"))
                        s['routes']=[r for r in s['routes'] if r['name']!=name]
                    self.change_state(rem)
                elif choice=="3":
                    state=self.get_states()[0][1].get('state',{}); print(json.dumps(state.get('routes',[]),ensure_ascii=False,indent=2) or self.t("lb.list.empty")); input(self.t("lb.press_enter"))
                elif choice=="0": return
            except (KeyboardInterrupt,EOFError): return
            except Exception as e: print(self.t("lb.error.operation",str(e)))

    def nodes_menu(self):
        while True:
            print("\n"+self.t("lb.nodes.title")); print("1) "+self.t("lb.nodes.replace")); print("2) "+self.t("lb.nodes.add")); print("3) "+self.t("lb.nodes.remove")); print("4) "+self.t("lb.nodes.ssh_user")); print("0) "+self.t("common.menu.back"))
            choice=input(self.t("common.menu.choice")+": ").strip()
            try:
                if choice=="1":
                    raw=self.ask("lb.nodes.batch"); parsed=self.parse_nodes(raw)
                    if len(parsed)<2: raise ValueError(self.t("lb.error.need_pair"))
                    self.config['managed']=parsed; self.save(); print(self.t("lb.nodes.saved"))
                elif choice=="2":
                    parsed=self.parse_nodes(self.ask("lb.nodes.batch"))
                    have={n['name'] for n in self.nodes()}
                    for n in parsed:
                        if n['name'] not in have: self.nodes().append(n)
                    self.save(); print(self.t("lb.nodes.saved"))
                elif choice=="3":
                    print(", ".join(n['name']+'='+n['ip'] for n in self.nodes()))
                    name=self.ask("lb.nodes.remove_prompt"); proposed=[n for n in self.nodes() if n['name']!=name]
                    if len(proposed)<2: raise ValueError(self.t("lb.error.need_pair"))
                    self.config['managed']=proposed; self.save(); print(self.t("lb.nodes.saved"))
                elif choice=="4":
                    user=self.ask("lb.nodes.ssh_user_prompt",self.config.get('sshUser','ubuntu'))
                    if not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?",user): raise ValueError(self.t("lb.error.ssh_user"))
                    self.config['sshUser']=user; self.save(); print(self.t("lb.nodes.saved"))
                elif choice=="0": return
            except (KeyboardInterrupt,EOFError): return
            except Exception as e: print(self.t("lb.error.operation",str(e)))

    def ipv4(self, value):
        try: return str(ipaddress.IPv4Address(value))
        except ipaddress.AddressValueError: raise ValueError(self.t("lb.error.ipv4")) from None

    def parse_nodes(self, raw):
        result=[]
        for entry in re.split(r"[\s,;]+",raw.strip()):
            if not entry: continue
            name,sep,ip=entry.partition("=")
            if not sep or not re.fullmatch(r"lb[a-z0-9-]*",name): raise ValueError(self.t("lb.error.node_format"))
            result.append({'name':name,'ip':self.ipv4(ip)})
        if len({x['name'] for x in result})!=len(result) or len({x['ip'] for x in result})!=len(result): raise ValueError(self.t("lb.error.node_duplicate"))
        return result

    def external_menu(self):
        while True:
            print("\n"+self.t("lb.external.title")); print("1) "+self.t("lb.external.add")); print("2) "+self.t("lb.external.remove")); print("3) "+self.t("lb.external.list")); print("0) "+self.t("common.menu.back"))
            choice=input(self.t("common.menu.choice")+": ").strip()
            try:
                if choice=="1":
                    name=self.valid_name(self.ask("lb.external.name")); endpoint=self.ask("lb.external.endpoint")
                    if not re.match(r"^https?://",endpoint) and not re.match(r"^[A-Za-z0-9.-]+(:[0-9]{1,5})?$",endpoint): raise ValueError(self.t("lb.error.endpoint"))
                    self.config['external']=[x for x in self.config['external'] if x['name']!=name]+[{'name':name,'endpoint':endpoint}]; self.save(); print(self.t("lb.external.saved"))
                elif choice=="2":
                    print("\n".join(x['name']+' '+x['endpoint'] for x in self.config['external']) or self.t("lb.list.empty")); name=self.ask("lb.external.remove_prompt")
                    self.config['external']=[x for x in self.config['external'] if x['name']!=name]; self.save(); print(self.t("lb.external.saved"))
                elif choice=="3":
                    print("\n".join(x['name']+' '+x['endpoint'] for x in self.config['external']) or self.t("lb.list.empty")); input(self.t("lb.press_enter"))
                elif choice=="0": return
            except (KeyboardInterrupt,EOFError): return
            except Exception as e: print(self.t("lb.error.operation",str(e)))

    def language_menu(self):
        locales=i18n.available_locales(); print(self.t("common.language.prompt"))
        for idx,(code,name) in enumerate(locales.items(),1): print(f"{idx}) {i18n.translate(self.lang,'common.language.'+code.lower())} ({code})")
        choice=input(self.t("common.menu.choice")+": ").strip()
        codes=list(locales)
        if choice.isdigit() and 1<=int(choice)<=len(codes): self.lang=codes[int(choice)-1]
        elif choice in locales: self.lang=choice
        print(i18n.translate(self.lang,"common.language.selected",self.lang))

    def run(self):
        print(self.t("lb.warning.test"))
        print(self.t("lb.warning.route_scope"))
        while True:
            print("\n"+self.t("lb.menu.title")); print("1) "+self.t("lb.menu.status")); print("2) "+self.t("lb.menu.vips")); print("3) "+self.t("lb.menu.routes")); print("4) "+self.t("lb.menu.nodes")); print("5) "+self.t("lb.menu.external")); print("L) "+self.t("common.language.prompt")); print("0) "+self.t("common.menu.exit"))
            choice=input(self.t("common.menu.choice")+": ").strip().lower()
            try:
                if choice=="1": self.dashboard()
                elif choice=="2": self.vips_menu()
                elif choice=="3": self.route_menu()
                elif choice=="4": self.nodes_menu()
                elif choice=="5": self.external_menu()
                elif choice=="l": self.language_menu()
                elif choice=="0": return
            except Exception as e: print(self.t("lb.error.operation",str(e)))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lang',default='en'); parser.add_argument('--refresh',type=int,default=3)
    args=parser.parse_args()
    try:
        if args.refresh<1 or args.refresh>60: raise ValueError("refresh must be 1..60")
        App(args.lang,args.refresh).run()
    except (i18n.LocalizationError,ValueError) as e:
        print(str(e),file=sys.stderr); return 2
    return 0
if __name__=='__main__': raise SystemExit(main())
