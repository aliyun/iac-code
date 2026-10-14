"""Route matching binds account resources to their own API request context, not a fake physical region."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "case",
    [
        "account",
        "alias",
        "regional",
        "wrong_request_region",
        "missing_request_region",
        "wrong_product",
        "wrong_action",
        "split_sources",
        "missing_scope",
        "regional_without_region",
        "conflicting_own_region",
        "empty_own_region",
        "wrong_parent",
        "wrong_identifier",
        "wrong_type",
        "wrong_partition",
        "missing_account",
        "deleted",
        "l1",
        "wrong_contract",
        "excluded",
    ],
)
def test_browser_route_account_source_context(case):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = Path(__file__).parents[2] / "scripts/web/e2e/resource_link_chrome_batch.mjs"
    javascript = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),crypto=require('crypto');
const source=fs.readFileSync(process.argv[1],'utf8'),kind=process.argv[2];
const code=source.slice(source.indexOf('function sourceMatches('),source.indexOf('async function selectReference('));
const hash=s=>crypto.createHash('sha256').update(s).digest('hex');
const match=vm.runInNewContext(code+';({referenceMatchesPlan,routeReferenceRegionId})',{sha256:hash});
const ref={resourceType:'ALIYUN::PVTZ::ZoneRecord',identifier:'record-one',
 scopeKind:'account',accountScope:'credential:fake',consolePartition:'aliyun',
 attributes:{ZoneId:'zone-one'},sources:[{product:'pvtz',action:'DescribeZoneRecords',requestRegionId:'cn-hangzhou'}]};
const item={sourceType:'alicloud_pvtz_zone_record',canonicalResourceType:ref.resourceType,receiptSource:'route_catalog',
 actualApiCalled:true,sampleLevel:'L2',contractId:'route_catalog:alicloud_pvtz_zone_record:aliyun',
 product:'pvtz',action:'DescribeZoneRecords',sourceEvidenceDigest:'a'.repeat(64),
 routeSourceBinding:{regionId:'cn-hangzhou',resourceIdentifierDigests:[hash(ref.identifier)],parentAttributes:{ZoneId:'zone-one'}}};
const positive=['account','alias','regional'];
if(kind==='alias')ref.resourceType=item.sourceType;
if(kind==='regional'){ref.scopeKind='regional';ref.regionId='cn-hangzhou';}
if(kind==='wrong_request_region')ref.sources[0].requestRegionId='cn-beijing';
if(kind==='missing_request_region')delete ref.sources[0].requestRegionId;
if(kind==='wrong_product')ref.sources[0].product='ecs';
if(kind==='wrong_action')ref.sources[0].action='DescribeZones';
if(kind==='split_sources')ref.sources=[{product:'pvtz',action:'DescribeZoneRecords',requestRegionId:'cn-beijing'},
 {product:'ecs',action:'DescribeInstances',requestRegionId:'cn-hangzhou'}];
if(kind==='missing_scope')delete ref.scopeKind;
if(kind==='regional_without_region')ref.scopeKind='regional';
if(kind==='conflicting_own_region')ref.regionId='cn-beijing';
if(kind==='empty_own_region')ref.regionId='';
if(kind==='wrong_parent')ref.attributes.ZoneId='zone-two';
if(kind==='wrong_identifier')ref.identifier='record-two';
if(kind==='wrong_type')ref.resourceType='ALIYUN::RAM::Group';
if(kind==='wrong_partition')ref.consolePartition='alibabacloud';
if(kind==='missing_account')delete ref.accountScope;
if(kind==='deleted')ref.status='DELETE_COMPLETE';
if(kind==='l1'){item.actualApiCalled=false;item.sampleLevel='L1';}
if(kind==='wrong_contract')item.contractId='query:borrowed';
if(kind==='excluded')item.excludedSampleContexts=[{sessionIdDigest:hash('ws~own'),accountScopeDigest:hash(ref.accountScope),
 resourceIdentifierDigest:hash(ref.identifier),regionId:null,sourceEvidenceDigest:item.sourceEvidenceDigest}];
assert.equal(match.referenceMatchesPlan(ref,item,'ws~own'),positive.includes(kind));
if(positive.includes(kind)){
 assert.equal(match.routeReferenceRegionId(ref,item),'cn-hangzhou');
 if(kind!=='regional')assert.equal(ref.regionId,undefined,'do not mutate an account resource into regional');
}
assert(source.includes('regionId: item.receiptSource === "route_catalog"'));
assert(source.includes('? routeReferenceRegionId(reference, item) : reference.regionId ||'));
assert(source.includes('queryReferenceRegionId(reference, item,'));
console.log('passed');
"""
    result = subprocess.run(
        [node, "-e", javascript, str(script), case],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "passed"
