const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const mobile=require('../android/assets/mobile/mobile-core.js');
const source=fs.readFileSync(path.join(__dirname,'../src/supermonoagent/static/workspace.js'),'utf8');
const desktop=Function('C',source.slice(source.indexOf('  function agentType('),source.indexOf('  function renderTree('))+';return agentType;')(require('../src/supermonoagent/static/terminal-client.js'));

test('both choosers identify programming agents rather than their parent shell',()=>{
  const cases=[
    [{shell:'powershell',title:'Fix with Codex'},'', 'Codex'],
    [{shell:'cmd'},'Claude Code project','Claude Code'],
    [{shell:'claude'},'', 'Claude Code'],
    [{shell:'pwsh',argv:['node','C:/cli/codex.js']},'', 'Codex'],
    [{shell:'cmd',command:'kimi --resume'},'', 'Kimi'],
    [{shell:'powershell',agent:'Gemini',title:'old Codex task'},'', 'Gemini'],
    [{shell:'powershell',learnedAgent:'codex',title:'Build task'},'', 'Codex'],
    [{shell:'powershell',agent:'Gemini',learnedAgent:'codex'},'', 'Gemini'],
    [{shell:'opencode'},'', 'OpenCode'],
    [{shell:'powershell',title:'Build'},'', '其他'],
    [{shell:'cmd'},'', '其他'],
    [{shell:'powershell',title:'decodex'},'', '其他'],
  ];
  for(const [session,title,label] of cases){
    assert.equal(mobile.agentType(session,title),label);
    assert.equal(desktop(session,title),label);
  }
});
