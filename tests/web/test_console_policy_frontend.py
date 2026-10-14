"""Offline settings interaction; no browser login, credentials or cloud calls."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_permission_repair_requires_confirmation_and_saved_target(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    module = Path(__file__).parents[2] / "src/iac_code/web/static/js/components/workspace.js"
    script = tmp_path / "console-policy.mjs"
    script.write_text(
        "import { createWorkspaceController } from "
        + json.dumps(module.as_uri())
        + ";\n"
        + r"""
import assert from 'node:assert/strict';
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attributes = {};
    this.listeners = {}; this.textContent = ''; this.value = ''; this.className = ''; this.style = {};
    this.open = false;
    this.classList = {toggle(){}, add(){}, remove(){}, contains(){return false;}};
  }
  append(...children){ this.children.push(...children); }
  replaceChildren(...children){ this.children = children; }
  setAttribute(key, value) {
    this.attributes[key] = value;
    if (key.startsWith('data-')) this.dataset[key.slice(5).replace(/-([a-z])/g, (_,c)=>c.toUpperCase())] = value;
  }
  addEventListener(type, handler){ (this.listeners[type] ||= []).push(handler); }
  async click(){ if (!this.disabled) for (const fn of this.listeners.click || []) await fn({target:this}); }
  querySelectorAll(selector) {
    const nodes = []; const visit = (node) => {
      const data = selector.match(/^\[data-([\w-]+)(?:="([^"]*)")?\]$/);
      const key = data?.[1].replace(/-([a-z])/g, (_,c)=>c.toUpperCase());
      if (data && key in node.dataset && (data[2] === undefined || data[2] === node.dataset[key])) nodes.push(node);
      if (selector.startsWith('.') && node.className.split(/\s+/).includes(selector.slice(1))) nodes.push(node);
      for (const child of node.children || []) if (child instanceof Element) visit(child);
    }; visit(this); return nodes;
  }
  querySelector(selector){ return this.querySelectorAll(selector)[0] || null; }
  get options(){ return this.children; }
}
globalThis.document = {createElement: tag => new Element(tag), documentElement: {dataset:{}}, addEventListener(){}};
let consent = false; const confirmations = []; const calls = [];
globalThis.window = {confirm: text => { confirmations.push(text); return consent; }};
const arn = 'acs:ram::1001:role/iaccodeconsolesigninrole';
const state = {roleArn:arn, configured:true, supplementPolicy:{name:'IacCodeConsoleReadOnlySupplement',
  revision:'a'.repeat(64), needsRepair:true, actions:['oos:SearchInventory','odps:CheckTunnelAdditional']}};
const api = {
  getConsoleSigninRole: async () => structuredClone(state),
  repairConsoleSigninRole: async (...args) => { calls.push(args); return {status:'ready'}; },
  createConsoleSigninRole: async () => { calls.push(['create']); return {status:'ready',roleArn:arn}; },
  getForeignSessionsVisibility: async () => ({}), getSellingReviewStep: async () => ({}),
  getTelemetrySettings: async () => ({}), getAppearance: async () => ({}), getUiLanguage: async () => ({}),
  getDeveloperSettings: async () => ({}),
};
const content = new Element('div');
const controller = createWorkspaceController({tabs:new Element('nav'),content}, api);
controller.setActiveTab('other');
for(let i=0;i<8;i++) await new Promise(resolve=>setTimeout(resolve,0));
const find = name => content.querySelector(`[data-workspace-action="${name}"]`);
const repair = find('console-signin-role-repair');
const input = find('workspace-console-signin-role-arn');
const disclosure = find('console-signin-role-policy');
assert.ok(disclosure); assert.equal(disclosure.tagName,'DETAILS');
assert.equal(disclosure.open,false); assert.equal(disclosure.hidden,false);
assert.equal(disclosure.children[0].tagName,'SUMMARY');
assert.ok(disclosure.children[0].textContent.includes('Additional read-only permissions'));
const policyContent = disclosure.children[1];
assert.equal(policyContent.tagName,'DIV');
assert.equal(policyContent.children[0].tagName,'H5');
assert.equal(policyContent.children[0].textContent,'IacCodeConsoleReadOnlySupplement');
const actionList = policyContent.children[1];
assert.equal(actionList.tagName,'UL');
assert.equal(actionList.children.length,2);
assert.ok(actionList.children.every(item=>item.tagName==='LI' && item.children[0].tagName==='CODE'));
assert.deepEqual(actionList.children.map(item=>item.children[0].textContent),state.supplementPolicy.actions);
assert.ok(repair); assert.equal(repair.disabled,false); assert.equal(calls.length,0);
await repair.click(); assert.equal(calls.length,0); assert.equal(confirmations.length,1);
assert.ok(confirmations[0].includes(arn)); assert.ok(confirmations[0].includes('oos:SearchInventory'));
assert.ok(confirmations[0].includes('Additional read-only permissions'));
assert.ok(confirmations[0].includes(
  'IacCodeConsoleReadOnlySupplement\n  oos:SearchInventory\n  odps:CheckTunnelAdditional'
));
assert.equal(disclosure.open,false);
disclosure.open = true;
consent = true; input.value = 'acs:ram::1001:role/other';
await repair.click(); assert.equal(calls.length,0); assert.equal(confirmations.length,1);
input.value = arn; await repair.click();
assert.deepEqual(calls,[[arn,'a'.repeat(64)]]); assert.equal(repair.disabled,false);
assert.equal(disclosure.open,true);
window.confirm = undefined; await repair.click(); assert.equal(calls.length,1);
window.confirm = text => { confirmations.push(text); return false; };
await find('console-signin-role-create').click();
assert.equal(calls.length,1);
assert.ok(confirmations.at(-1).includes('IacCodeConsoleSigninRole'));
assert.ok(!confirmations.at(-1).includes(arn));
console.log('ok');
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
