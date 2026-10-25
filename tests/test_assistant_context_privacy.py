"""Cropping must not turn a redactable credential into a leaked fragment."""
import json
from pathlib import Path
import subprocess

import pytest

from test_assistant_service import service


NODE = r'''
const fs=require('node:fs'),source=fs.readFileSync(process.argv[1],'utf8');
const start=source.indexOf('  function context(pane){'),end=source.indexOf('  async function refreshPreferences()');
if(start<0||end<start)throw Error('Missing production context helpers');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const helpers=new Function('contextLimit',source.slice(start,end)+';return {clip,context,evidenceContext};')(input.limit);
let output;
if(input.kind==='suffix')output=helpers.clip(input.text,input.size,true);
else if(input.kind==='rows'){
  const rows=input.rows;
  output=helpers.context({term:{buffer:{active:{baseY:0,cursorY:rows.length-1,getLine:i=>({isWrapped:rows[i].wrapped,translateToString:()=>rows[i].text})}}}});
}else output=helpers.evidenceContext(input.terminal||'',{project_source:'session-startup-directory',project:'owned',ledger:{enabled:true,pages:input.pages}});
process.stdout.write(JSON.stringify(output));
'''


@pytest.mark.parametrize('boundary', ['prefix1024', 'prefix128', 'budget', 'suffix', 'backend', 'rows', 'wrapped'])
def test_context_crop_preserves_native_credential_redaction(tmp_path, boundary):
    key = 'OWNED-9qX7-"\\-R4t6-SeCrEt'
    args = {'kind': 'pages', 'limit': 8192, 'pages': {}}
    if boundary in {'prefix1024', 'prefix128'}:
        size = 128 if boundary == 'prefix128' else 1024
        args['limit'] = 512 if size == 128 else 8192
        text = 'x' * (size - len(key)) + ' ' + key + ' tail'
        args['pages']['negatives.md'] = {'status': 'ready', 'text': text, 'mtime': 1}
    elif boundary == 'budget':
        args['limit'] = 2048
        text = 'x' * (512 - len(key)) + ' ' + key + ' tail ' + ' y' * 600
        for name in ('negatives.md', 'trials.md', 'nodes.md', 'ideas.md', 'README.md'):
            args['pages'][name] = {'status': 'ready', 'text': text, 'mtime': 1}
    elif boundary == 'suffix':
        text = key + ' safe evidence ' + 'x' * 500
        args.update(kind='suffix', text=text, size=len(text) - 1)
    elif boundary == 'backend':
        args['pages']['negatives.md'] = {'status': 'ready', 'text': 'safe evidence ' + key[:-1], 'truncated': True}
    elif boundary == 'rows':
        rows = [{'text': key[:1], 'wrapped': False}, {'text': key[1:10], 'wrapped': True},
                {'text': key[10:], 'wrapped': True}, {'text': ' safe evidence', 'wrapped': True}]
        rows += [{'text': 'evidence ' + str(i), 'wrapped': False} for i in range(197)]
        args.update(kind='rows', rows=rows)
    else:
        args.update(kind='rows', rows=[{'text': key[:10], 'wrapped': False},
                    {'text': key[10:], 'wrapped': True}, {'text': ' safe evidence', 'wrapped': True}])
    script = Path('src/supermonoagent/static/assistant-chat.js').resolve()
    result = subprocess.run(['node', '-e', NODE, str(script)], input=json.dumps(args),
                            capture_output=True, text=True, encoding='utf-8', timeout=10)
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)
    assert len(context) <= (args['limit'] if args['kind'] == 'pages' else 32768)
    sent = []
    runner = service(tmp_path, query=lambda _endpoint, _model, messages, **_kw: sent.append(messages) or 'answer')
    runner.set_api_key(key)
    runner.chat({'question': 'Use evidence only', 'context': context, 'attach_context': True})
    delivered = json.loads(sent[0][-1]['content'])['terminal_context']
    assert key not in delivered
    assert key[:-1] not in delivered and key[1:] not in delivered
    assert key[:10] not in delivered and key[-10:] not in delivered
    if boundary in {'suffix', 'backend', 'rows', 'wrapped'}:
        assert 'safe evidence' in delivered
    if boundary == 'wrapped':
        assert '[凭据已隐藏]' in delivered
