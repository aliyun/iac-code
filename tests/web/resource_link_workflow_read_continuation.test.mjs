import test from 'node:test';
import assert from 'node:assert/strict';
import {allowedNaturalRequest,allowedOriginalReadContinuation} from '../../scripts/web/e2e/resource_link_workflow/browser.mjs';

const entry={id:'own-vpc-absence-cleanup',remaining:31,
  call:{product:'vpc',action:'DescribeVpcAttribute',version:'2016-04-28',region_id:'cn-hangzhou',
    params:{RegionId:'cn-hangzhou',VpcId:'vpc-offline123456'}},
  read_policy:{phase:'cleanup',required_cleanup:true,after_delete:'managed-stack-create',min_interval_seconds:30}};
const make=delivery=>'继续同一完整会话的原始响应绑定只读核查。禁止再次创建、删除。\n'+JSON.stringify(delivery);
const original=()=>({kind:'original_missing_cleanup_reads',calls:[structuredClone(entry)],
  absence_verified:false,allowance_changed:false});

test('exact original cleanup reads permit bound IDs only on the separate internal operation',()=>{
  const text=make(original());
  assert.equal(allowedNaturalRequest(text),false);
  assert.equal(allowedOriginalReadContinuation(text),true);
  assert.equal(allowedOriginalReadContinuation('继续原始只读核查，不创建或删除。'),true);
});

test('ID-free legacy continuations retain the original natural-language policy',()=>{
  const d=original();d.calls=[];
  assert.equal(allowedNaturalRequest(make(d)),true);
  assert.equal(allowedOriginalReadContinuation(make(d)),true);
});

for(const damage of ['write','version','region','parameter','unknown','url','type','quota','phase',
  'absence','allowance','duplicate','prefix','kind'])
  test(`untrusted or changed original read delivery is rejected: ${damage}`,()=>{
    const d=original(),row=d.calls[0];
    if(damage==='write') row.call.action='DeleteVpc';
    if(damage==='version') row.call.version='new';
    if(damage==='region') row.call.region_id='cn-shenzhen';
    if(damage==='parameter') row.call.params.Unreviewed='extra';
    if(damage==='unknown') row.call.action='CheckMaybeWrite';
    if(damage==='url') row.call.params.VpcId='https://example.invalid/vpc-offline123456';
    if(damage==='type') row.call.params.VpcId='ALIYUN::VPC::VPC';
    if(damage==='quota') row.remaining=0;
    if(damage==='phase') row.read_policy.phase='execute';
    if(damage==='absence') d.absence_verified=true;
    if(damage==='allowance') d.allowance_changed=true;
    if(damage==='duplicate') d.calls.push(structuredClone(row));
    if(damage==='kind') d.kind='model_supplied_calls';
    const text=(damage==='prefix'?'https://example.invalid/vpc-offline123456\n':'')+make(d);
    assert.equal(allowedOriginalReadContinuation(text),false);
  });
