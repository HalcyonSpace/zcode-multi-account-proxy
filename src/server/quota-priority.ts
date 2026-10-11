import { readFile } from 'node:fs/promises';

type Account = {id:string;port:number;apiKey:string};
type Candidate = {account:Account; free:boolean; expires:number};
let cached: {until:number; items: {account:Account; quota:any}[]} | undefined;
const cooldown = new Map<string,number>();
export function freeQuota(q:any,model:string,now:number):number|null {
 const dates=(q.plans||[]).map((p:any)=>p.endsAt).filter((n:any)=>typeof n==='number');
 if(dates.length && dates.every((n:number)=>n<=now))return null;
 const rows=(q.balances||[]).filter((b:any)=>String(b.showName||'').toLowerCase().replace(/[^a-z0-9]/g,'')===model.toLowerCase().replace(/[^a-z0-9]/g,'') && Number(b.remainingUnits)>0 && (!b.expiresAt||Number(b.expiresAt)>now));
 return rows.length?Math.min(...rows.map((b:any)=>b.expiresAt||Infinity)):null;
}
async function inventory():Promise<{account:Account;quota:any}[]> {
 if(cached && cached.until>Date.now())return cached.items;
 const path=process.env.ZCODE_FLEET_REGISTRY;if(!path)return [];
 const registry=JSON.parse(await readFile(path,'utf8'));
 const items=await Promise.all(registry.instances.map(async(account:Account)=>{
  try{const r=await fetch(`http://127.0.0.1:${account.port}/quota`,{headers:{authorization:`Bearer ${account.apiKey}`},signal:AbortSignal.timeout(4000)});return r.ok?{account,quota:await r.json()}:null;}catch{return null;}
 }));
 const valid=items.filter(Boolean);cached={until:Date.now()+10000,items:valid};return valid;
}
/** All chat entrypoints share one policy. Internal forwarding bypasses this layer. */
export async function quotaPriority(req:Request,local:()=>Promise<Response>):Promise<Response> {
 if(!process.env.ZCODE_FLEET_REGISTRY||(process.env.ZCODE_FLEET_INTERNAL_SECRET && req.headers.get('x-zcode-fleet-internal')===process.env.ZCODE_FLEET_INTERNAL_SECRET))return local();
 const body=await req.clone().json().catch(()=>null);if(!body?.model)return local();
 const model=String(body.model), now=Date.now()/1000;
 const candidates:Candidate[]=[];
 for(const item of await inventory()){
  const key=item.account.id+':'+model;if((cooldown.get(key)||0)>Date.now())continue;
  const expiry=item.quota.plan==='coding-plan'?null:freeQuota(item.quota,model,now);
  if(expiry!==null)candidates.push({account:item.account,free:true,expires:expiry});
  else if(item.quota.codingPlan?.level && !(item.quota.codingPlan.limits||[]).some((l:any)=>Number(l.percentage)>=100))candidates.push({account:item.account,free:false,expires:Infinity});
 }
 candidates.sort((a,b)=>Number(b.free)-Number(a.free)||a.expires-b.expires||a.account.id.localeCompare(b.account.id));
 if(!candidates.length)return new Response(JSON.stringify({error:{type:'quota_unavailable',message:'No available free or paid account for the requested model'}}),{status:503,headers:{'content-type':'application/json'}});
 for(const c of candidates){
  const headers=new Headers(req.headers);headers.set('authorization',`Bearer ${c.account.apiKey}`);headers.delete('x-api-key');headers.set('x-zcode-fleet-internal',process.env.ZCODE_FLEET_INTERNAL_SECRET||'');headers.delete('host');headers.delete('content-length');
  try{
   const response=await fetch(`http://127.0.0.1:${c.account.port}${new URL(req.url).pathname}`,{method:'POST',headers,body:JSON.stringify(body),signal:AbortSignal.timeout(120000)});
   if(!response.ok){
    const text=await response.clone().text();
    if([401,403,429].includes(response.status)||(/quota|rate.?limit|code.?[": ]+1005/i.test(text))){cooldown.set(c.account.id+':'+model,Date.now()+30000);await response.body?.cancel();cached=undefined;continue;}
   }
   const out=new Headers(response.headers);out.set('x-zcode-account',c.account.id);out.set('x-zcode-quota-tier',c.free?'free':'paid');
   return new Response(response.body,{status:response.status,headers:out});
  }catch(e){
   // Network ambiguity: don't replay a potentially billed request.
   return new Response(JSON.stringify({error:{type:'fleet_transport_error',message:'Selected account transport failed; request not replayed'}}),{status:502,headers:{'content-type':'application/json'}});
  }
 }
 return new Response(JSON.stringify({error:{type:'quota_unavailable',message:'All eligible accounts rejected quota or authentication'}}),{status:503,headers:{'content-type':'application/json'}});
}
