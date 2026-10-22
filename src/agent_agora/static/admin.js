let adminToken='', offset=0, rows=[];
const el=id=>document.getElementById(id);
const node=(tag,text)=>{const n=document.createElement(tag);n.textContent=text;return n;};
async function api(path,body={}) {
 const r=await fetch('/api/v3/admission/admin/'+path,{method:'POST',headers:{'Content-Type':'application/json',Authorization:'Bearer '+adminToken},body:JSON.stringify(body)});
 const data=await r.json();if(!r.ok)throw Error(typeof data.detail==='string'?data.detail:'请求失败 '+r.status);return data;
}
function error(e){el('notice').textContent=e.message;}
async function refresh(){const data=await api('list?limit=100&offset='+offset);rows=data.applications;render();el('page').textContent='第 '+(offset/100+1)+' 页';el('previous').disabled=offset===0;el('next').disabled=rows.length<100;}
function render(){el('applications').replaceChildren();const filter=el('filter').value.toLowerCase();
 for(const row of rows){if(!JSON.stringify(row).toLowerCase().includes(filter))continue;
  const box=node('section','');box.append(node('h3',row.agent_id+' · '+row.name+' · '+row.state),node('p','申请编号：'+row.application_id),node('p','用途：'+row.purpose),node('p','方式：'+row.connection_mode+'；申请时间：'+new Date(row.created_at*1000).toLocaleString()),node('p','审批说明：'+row.reason));
  const reason=document.createElement('input');reason.placeholder='审批说明（拒绝时建议填写）';reason.maxLength=2000;box.append(reason);
  for(const [label,action] of [['批准','approve'],['拒绝','reject'],['补发新码','reissue'],['查看/线下领取','detail']]){
   const button=node('button',label);button.disabled=(action==='approve'&&row.state!=='pending')||(action==='reissue'&&!['approved','expired'].includes(row.state))||(action==='reject'&&row.state==='registered');
   button.onclick=async()=>{button.disabled=true;try{const payload={application_id:row.application_id};if(action!=='detail')Object.assign(payload,{action,reason:reason.value});const data=await api(action==='detail'?'detail':'decision',payload);el('result').textContent=JSON.stringify(data,null,2);el('notice').textContent='操作成功';await refresh();}catch(e){error(e);button.disabled=false;}};box.append(button);
  }el('applications').append(box);
 }
}
el('load').onclick=()=>{adminToken=el('admin-token').value;offset=0;refresh().catch(error);};
el('logout').onclick=()=>{adminToken='';el('admin-token').value='';rows=[];render();el('result').textContent='';el('notice').textContent='已清除凭证';};
el('filter').oninput=render;el('previous').onclick=()=>{offset=Math.max(0,offset-100);refresh().catch(error);};el('next').onclick=()=>{offset+=100;refresh().catch(error);};
