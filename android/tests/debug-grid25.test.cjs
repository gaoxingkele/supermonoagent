const {test}=require('node:test');
const assert=require('node:assert/strict');
const {debugGridPage,debugSortEntries,debugSwipePage,debugRecoveryFeed}=require('../assets/mobile/mobile-core.js');

test('25-cell pages include every unique target and clamp after removal',()=>{
  const rows=Array.from({length:56},(_,i)=>({key:String(i)}));
  assert.equal(debugGridPage([...rows,rows[0]],0,25).items.length,25);
  assert.deepEqual(debugGridPage(rows,2,25).items,rows.slice(50));
  assert.equal(debugGridPage(rows.slice(0,2),2,25).page,0);
  assert.equal(debugGridPage(rows,0,9).items.length,9);
  assert.equal(debugGridPage(rows,0,16).items.length,16);
});

test('active targets lead, idle updates descend, and ties stay stable',()=>{
  const rows=[{key:'unknown',updatedAt:0,order:0},{key:'old',updatedAt:10,order:1},
    {key:'active',active:true,updatedAt:1,order:2},{key:'new',updatedAt:20,order:3},
    {key:'equal',updatedAt:20,order:4},{key:'invalid',updatedAt:NaN,order:5}];
  assert.deepEqual(debugSortEntries(rows).map(row=>row.key),['active','new','equal','old','unknown','invalid']);
  assert.deepEqual(debugSortEntries([{key:'a'},{key:'b'}]).map(row=>row.key),['a','b']);
  assert.equal(rows[0].key,'unknown','Sorting must not mutate the directory');
});

test('swipe pagination ignores vertical drags and taps, and cannot escape boundaries',()=>{
  assert.deepEqual(debugSwipePage({x:180,y:100},{x:30,y:110},0,3),{page:1,swiped:true});
  assert.deepEqual(debugSwipePage({x:30,y:100},{x:180,y:110},1,3),{page:0,swiped:true});
  assert.deepEqual(debugSwipePage({x:30,y:100},{x:180,y:110},0,3),{page:0,swiped:true});
  assert.deepEqual(debugSwipePage({x:180,y:100},{x:30,y:110},2,3),{page:2,swiped:true});
  assert.equal(debugSwipePage({x:0,y:0},{x:50,y:70},1,3).swiped,false);
  assert.equal(debugSwipePage({x:0,y:0},{x:12,y:3},1,3).swiped,false);
});

test('passive recovered footers do not pretend to be a live update',()=>{
  const bytes=value=>new TextEncoder().encode(value);
  assert.equal(debugRecoveryFeed(bytes('\x1b]7501;app=codex\x07\x1b7\x1b[?6l\x1b[40;1HContext 50% Weekly 70%\x1b[0m\x1b8')),true);
  assert.equal(debugRecoveryFeed(bytes('real new output')),false);
  assert.equal(debugRecoveryFeed(bytes('\x1b7ordinary saved-cursor text\x1b8')),false);
});
