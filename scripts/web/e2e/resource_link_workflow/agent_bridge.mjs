/** Per-job loopback transport for ordinary script calls, not Codex dynamicTools. */
import http from 'node:http';
import {randomBytes, timingSafeEqual, createHash} from 'node:crypto';
import {spawn} from 'node:child_process';

export async function startAgentBridge({post,record,binding,ready}) {
  const token=randomBytes(32).toString('hex');
  let busy=false,active=true;
  const server=http.createServer(async(req,res)=>{
    const reply=(status,value)=>{res.writeHead(status,{'content-type':'application/json'});res.end(JSON.stringify(value));};
    const supplied=Buffer.from(req.headers.authorization || ''),expected=Buffer.from('Bearer '+token);
    if(req.method!=='POST'||req.url!=='/invoke'||supplied.length!==expected.length||
      !timingSafeEqual(supplied,expected)) {reply(403,{ok:false,error:'job bridge authorization rejected'});return;}
    if(busy||!active) {reply(409,{ok:false,error:'job bridge inactive or operation already in flight; do not retry'});return;}
    busy=true;
    let operation;
    try{
      let bytes=0,body='';
      for await(const chunk of req){bytes+=chunk.length;if(bytes>2_000_000)throw Error('invalid request');body+=chunk;}
      const request=JSON.parse(body);
      if(!request||Object.keys(request).sort().join(',')!=='arguments,operation'||
        !/^[a-z][a-z0-9_-]*$/.test(request.operation)||
        !request.arguments||typeof request.arguments!=='object'||Array.isArray(request.arguments))throw Error('invalid request');
      operation=request.operation;
      await ready;
      const exact=binding();
      if(!active||!exact.threadId||!exact.turnId)throw Error('binding unavailable');
      record('script/request',{...exact,operation,arguments:request.arguments});
      let result;
      try {result=await post(operation,request.arguments);}
      catch(error){
        if(error.name!=='OperationRejected')throw error;
        record('script/rejected',{...exact,operation,error:error.message});
        reply(422,{ok:false,outcome:'rejected',error:error.message});return;
      }
      const {image_url,...metadata}=result;
      record('script/result',{...exact,operation,result:metadata,
        ...(image_url?{image_payload_sha256:createHash('sha256').update(image_url).digest('hex')}: {})});
      reply(200,{ok:true,result});
    }catch{
      record('script/transport_error',{...binding(),operation,outcome:'unknown_or_not_started',do_not_retry:true});
      reply(502,{ok:false,outcome:'unknown_or_not_started',error:'preserve original job; reconcile before retry'});
    }finally{busy=false;}
  });
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  return {token,origin:`http://127.0.0.1:${server.address().port}`,port:server.address().port,
    close:async()=>{active=false;server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}};
}

export function shellQuote(value){return "'"+value.replaceAll("'","'\\''")+"'";}

export async function provisionAgentClient({root,jobId,config,bridge}) {
  const process=spawn(config.uv || 'uv',['run','python','-m',
    'scripts.web.e2e.resource_link_workflow.agent_transport',root,jobId,String(bridge.port)],
  {cwd:config.repo,stdio:['pipe','pipe','pipe']});
  const stop=()=>{process.stdin.end();};
  // Only this per-job bridge credential is delivered over stdin. Server/global
  // capability tokens and cloud credentials never leave the local adapter.
  process.stdin.write(JSON.stringify({origin:bridge.origin,token:bridge.token})+'\n');
  process.stdin.on('error',()=>{});
  process.stderr.on('data',()=>{}); // transport helper emits no secret diagnostics
  let stopping=false;
  const failure=new Promise((_,reject)=>{
    process.once('exit',()=>{if(!stopping)reject(Error('original script client transport lost; reconcile original turn'));});
    process.once('error',()=>reject(Error('original script client transport lost; reconcile original turn')));
  });
  failure.catch(()=>{});
  try{
    const result=await new Promise((resolve,reject)=>{
      let buffer='';
      const timer=setTimeout(()=>reject(Error('script client setup failed before turn/start')),45000);
      process.once('exit',()=>{clearTimeout(timer);reject(Error('script client setup failed before turn/start'));});
      process.once('error',()=>{clearTimeout(timer);reject(Error('script client setup failed before turn/start'));});
      process.stdout.on('data',chunk=>{
        buffer+=chunk;
        if(buffer.length>16000){clearTimeout(timer);reject(Error('invalid script client setup response'));return;}
        if(!buffer.includes('\n'))return;
        clearTimeout(timer);
        try{resolve(JSON.parse(buffer.split('\n')[0]));}catch{reject(Error('invalid script client setup response'));}
      });
    });
    if(result.ready!==true||typeof result.script!=='string'||!result.script.endsWith('/workflow-agent.py')||
      typeof result.python!=='string'||!/^[a-f0-9]{64}$/.test(result.script_sha256||''))
      throw Error('script client not ready before turn/start');
    return {command:`${shellQuote(result.python)} ${shellQuote(result.script)}`,sha256:result.script_sha256,failure,close:async()=>{
      stopping=true;
      stop();
      if(process.exitCode!==null)return;
      await new Promise(resolve=>{
        const timer=setTimeout(()=>{process.kill();resolve();},5000);
        process.once('exit',()=>{clearTimeout(timer);resolve();});
      });
    }};
  }catch(error){stopping=true;stop();process.kill();throw error;}
}
