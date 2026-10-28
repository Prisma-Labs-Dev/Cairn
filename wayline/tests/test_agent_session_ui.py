"""Exercise session-browser lifecycle behavior without model calls."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest


HTML = Path(__file__).parents[1] / "src/wayline/server/static/index.html"


def run_ui(code: str):
    if not shutil.which("node"):
        pytest.skip("Node is required for UI behavior regression tests")
    html = HTML.read_text()
    script = html[html.index("function waylineApp()"):].split("</script>")[0]
    harness = """
const assert = require('node:assert/strict');
const fs = require('node:fs');
const input = JSON.parse(fs.readFileSync(0,'utf8'));
eval(input.script);
(async () => { await eval('(async () => {'+input.code+'})()'); })().catch(error => { console.error(error); process.exitCode=1; });
"""
    result = subprocess.run(["node", "-e", harness], input=json.dumps({"script": script, "code": code}),
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


def test_closed_or_replaced_scope_ignores_old_list_responses():
    run_ui("""
    const app=waylineApp();
    const responses=[];
    app.api=(method,path)=>new Promise(resolve=>responses.push({path,resolve}));
    const first=app.openAgentSessions({project_id:'old'},'Old project');
    app.closeAgentSessions();
    const second=app.openAgentSessions({workflow_id:'new'},'New workflow');
    responses[1].resolve({items:[{id:'new-invocation'}],total:1,next_offset:null});
    await second;
    responses[0].resolve({items:[{id:'stale-invocation'}],total:1,next_offset:null});
    await first;
    assert.equal(app.agentSessions.title,'New workflow');
    assert.deepEqual(app.agentSessions.items.map(item=>item.id),['new-invocation']);
    assert.match(responses[0].path,/project_id=old/);
    assert.match(responses[1].path,/workflow_id=new/);
    assert.equal(app.agentSessions.loading,false);
    """)


def test_live_activity_uses_real_sequence_cursor_and_stays_after_completion():
    run_ui("""
    const sources=[];
    global.EventSource=class {
      constructor(url){this.url=url;this.listeners={};this.closed=false;sources.push(this);}
      addEventListener(type,callback){this.listeners[type]=callback;}
      close(){this.closed=true;}
    };
    const app=waylineApp();
    const paths=[];
    app.api=async(method,path)=>{paths.push(path);return {items:[{sequence:51,event:{type:'agent_start'}},{sequence:100,event:{type:'message_update',assistantMessageEvent:{type:'text_delta',delta:'actual Pi text'}}}],next_after:100,truncated:false};};
    app.agentSessions.open=true;
    await app.selectAgentSession({id:'one',status:'running',events_captured:100,capture_status:'capturing'});
    assert.match(paths[0],/after=50&limit=50/);
    assert.match(sources[0].url,/stream\\?after=100$/);
    sources[0].onopen();
    assert.equal(app.agentSessions.connection,'Browser event stream connected');
    assert.equal(app.agentSessionStatus(app.agentSessions.selected),'Process status unconfirmed');
    sources[0].listeners.update({data:JSON.stringify({session:{id:'other',status:'running'},events:[{sequence:101,event:{type:'other-session'}}],next_after:101})});
    assert.equal(app.agentSessions.events.length,2);
    sources[0].listeners.update({data:JSON.stringify({session:{id:'one',status:'success',capture_status:'complete'},events:[{sequence:101,event:{type:'agent_end'}}],next_after:101})});
    assert.equal(app.agentSessions.selected.status,'success');
    assert.equal(app.agentSessions.events.at(-1).event.type,'agent_end');
    assert.equal(app.agentSessions.connection,'Browser event stream connected');
    assert.equal(sources[0].closed,false);
    """)


def test_replaced_selection_ignores_old_events_and_closes_reconnects():
    run_ui("""
    const sources=[];const timers=[];
    global.EventSource=class {constructor(url){this.url=url;this.listeners={};sources.push(this);}addEventListener(type,fn){this.listeners[type]=fn;}close(){this.closed=true;}};
    global.setTimeout=fn=>{timers.push(fn);return timers.length;};
    global.clearTimeout=()=>{};
    const app=waylineApp();let resolveOld;
    app.api=(method,path)=>path.includes('/old/')?new Promise(resolve=>resolveOld=resolve):Promise.resolve({items:[{sequence:1,event:{type:'new-event'}}],next_after:1});
    app.agentSessions.open=true;
    const old=app.selectAgentSession({id:'old',status:'running'});
    await app.selectAgentSession({id:'new',status:'running'});
    resolveOld({items:[{sequence:1,event:{type:'old-event'}}],next_after:1});
    await old;
    assert.equal(sources.length,1);
    assert.match(sources[0].url,/new/);
    assert.equal(app.agentSessions.events[0].event.type,'new-event');
    sources[0].onerror();
    assert.equal(sources[0].closed,true);
    assert.equal(timers.length,1);
    app.closeAgentSessions();
    timers[0]();
    assert.equal(sources.length,1);
    assert.equal(app.agentSessions.open,false);
    """)


def test_activity_bounds_deduplication_and_graph_scope_are_explicit():
    run_ui("""
    const app=waylineApp();
    app.mergeAgentEvents(Array.from({length:180},(_,i)=>({sequence:i+1,event:{type:'message_update'}})));
    app.mergeAgentEvents([{sequence:180,event:{type:'agent_end'}},{sequence:-1,event:{type:'invalid'}}]);
    assert.equal(app.agentSessions.events.length,100);
    assert.equal(app.agentSessions.events[0].sequence,81);
    assert.equal(app.agentSessions.events.at(-1).event.type,'agent_end');
    assert.equal(app.agentSessions.nextAfter,180);
    assert.equal(app.agentSessionViewerUrl({viewer_url:'https://example.com/session?id=x'}),'');
    assert.equal(app.agentSessionViewerUrl({viewer_url:'//example.com/session?id=x'}),'');
    assert.equal(app.agentSessionViewerUrl({viewer_url:'/session?id=managed.jsonl'}),'/session?id=managed.jsonl');
    assert.equal(app.agentEventSummary({assistantMessageEvent:{delta:'<img src=x onerror=alert(1)>'}}),'<img src=x onerror=alert(1)>');
    assert.ok(app.agentEventDetail({value:'x'.repeat(9000)}).length<6100);
    let captured;
    app.openAgentSessions=(scope,title)=>{captured=scope;};
    app.selectedProjectId='project';app.project={project:{title:'Project'}};
    app.selectedNode={type:'intent',id:'edge-intent'};app.openGraphAgentSessions();
    assert.deepEqual(captured,{project_id:'project',intent_id:'edge-intent'});
    app.selectedNode={type:'fact',id:'source-fact'};app.openGraphAgentSessions();
    assert.deepEqual(captured,{project_id:'project',fact_id:'source-fact'});
    """)
    modal = HTML.read_text().split("<!-- Agent sessions -->", 1)[1].split("<!-- Toast -->", 1)[0]
    assert 'x-text="agentEventSummary(record.event)"' in modal
    assert "x-html" not in modal
    assert 'target="_blank" rel="noopener noreferrer"' in modal


def test_choice_alternatives_display_before_following_stage_without_requiring_them():
    run_ui("""
    const app=waylineApp();
    app.workflow={stages:[
      {id:'discover',depends_on:[],expansion:{mode:'one'},status:'completed'},
      {id:'serve',depends_on:['discover'],status:'completed'},
      {id:'pancakes',depends_on:['discover'],generated_by_stage_id:'discover',option_state:'unavailable',status:'stopped'},
      {id:'eggs',depends_on:['discover'],generated_by_stage_id:'discover',option_state:'succeeded',status:'completed'},
      {id:'benedict',depends_on:['discover'],generated_by_stage_id:'discover',option_state:'skipped',status:'stopped'},
    ]};
    const original=JSON.stringify(app.workflow);
    assert.deepEqual(app.workflowDisplayStages().map(stage=>stage.id),['discover','pancakes','eggs','benedict','serve']);
    assert.equal(JSON.stringify(app.workflow),original);
    assert.deepEqual(app.workflowRequiredStages().map(stage=>stage.id),['discover','serve','eggs']);
    assert.equal(app.workflowRequiredTotal(),3);
    assert.deepEqual(app.workflow.stages.find(stage=>stage.id==='serve').depends_on,['discover']);
    """)


def test_managed_conversation_and_original_pi_header_ids_remain_distinct():
    run_ui("""
    const app=waylineApp();
    const session={id:'invocation-uuid',conversation_id:'managed-conversation-uuid',pi_session_id:'preserved-pi-header-uuid',capture_status:'complete'};
    assert.equal(app.agentSessionPiId(session),'preserved-pi-header-uuid');
    assert.notEqual(app.agentSessionPiId(session),session.conversation_id);
    assert.equal(app.agentSessionPiId({conversation_id:'managed-only',capture_status:'waiting'}),'Waiting for Pi session header');
    assert.equal(app.agentSessionPiId({conversation_id:'managed-only',capture_status:'capturing'}),'Waiting for Pi session header');
    assert.equal(app.agentSessionPiId({conversation_id:'managed-only',capture_status:'unavailable'}),'Pi session header unavailable');
    """)
    modal = HTML.read_text().split("<!-- Agent sessions -->", 1)[1].split("<!-- Toast -->", 1)[0]
    assert '>Wayline conversation</dt>' in modal
    assert 'x-text="agentSessions.selected.conversation_id"' in modal
    assert '>Pi session ID</dt>' in modal
    assert 'x-text="agentSessionPiId(agentSessions.selected)"' in modal
    assert '>Invocation / usage ID</dt>' in modal


def test_stale_or_missing_dispatcher_confirmation_never_displays_running():
    run_ui("""
    const app=waylineApp();
    const fresh={status:'running',runtime_status:'running',capture_status:'capturing',runtime_last_reported_at:'2026-10-27T20:00:00Z'};
    const stale={...fresh,runtime_status:'unconfirmed'};
    const missing={status:'running',capture_status:'capturing'};
    assert.equal(app.agentSessionStatus(fresh),'Process running');
    assert.equal(app.agentSessionCaptureLabel(fresh),'Transcript updating');
    for (const session of [stale,missing]) {
      assert.equal(app.agentSessionStatus(session),'Process status unconfirmed');
      assert.equal(app.agentSessionCaptureLabel(session),'Transcript capture unconfirmed');
      assert.equal(app.agentSessionStatusClass(session),'bg-amber-50 text-amber-700');
    }
    assert.equal(app.agentSessionStatus({...stale,status:'interrupted',runtime_status:'interrupted'}),'Interrupted');
    assert.equal(app.agentSessionStatus({...stale,status:'success',runtime_status:'success'}),'Process finished');
    assert.equal(app.agentSessionCaptureLabel({...stale,capture_status:'complete'}),'Transcript upload pending');
    assert.equal(app.agentSessionCaptureLabel({...stale,capture_status:'complete',delivery_status:'delivered'}),'Transcript saved');
    """)
    modal = HTML.read_text().split("<!-- Agent sessions -->", 1)[1].split("<!-- Toast -->", 1)[0]
    assert 'Dispatcher has not confirmed this process recently; quiet activity alone does not prove it stopped.' in modal
    assert 'runtime_last_reported_at' in modal
    assert 'x-show="agentSessionRuntimeUnconfirmed(agentSessions.selected)"' in modal


def test_terminal_metadata_does_not_close_before_late_delivery_or_paged_events():
    run_ui("""
    const sources=[]; const timers=[];
    global.EventSource=class {constructor(url){this.url=url;this.listeners={};this.closed=false;sources.push(this);}addEventListener(type,fn){this.listeners[type]=fn;}close(){this.closed=true;}};
    global.setTimeout=fn=>{timers.push(fn);return timers.length;}; global.clearTimeout=()=>{};
    const app=waylineApp(); app.agentSessions.open=true;
    app.api=async()=>({items:[],next_after:0,truncated:false});
    // Final process/source metadata may arrive before offline raw/event replay.
    const terminal={id:'late',status:'success',capture_status:'complete',delivery_status:'pending',events_captured:0};
    await app.selectAgentSession(terminal);
    assert.equal(sources.length,1,'Terminal invocations still subscribe for replay');
    sources[0].onopen();
    sources[0].listeners.update({data:JSON.stringify({session:terminal,events:[],next_after:0})});
    assert.equal(sources[0].closed,false);
    assert.equal(app.agentSessionCaptureLabel(app.agentSessions.selected),'Transcript upload pending');
    const records=Array.from({length:75},(_,index)=>({sequence:index+1,event:{type:index===74?'agent_end':'message_update'}}));
    // Server emits at most 50 records per update; terminal metadata cannot stop
    // the remaining 25 from arriving on the same selected subscription.
    sources[0].listeners.update({data:JSON.stringify({session:{...terminal,events_captured:75},events:records.slice(0,50),next_after:50})});
    assert.equal(app.agentSessions.events.length,50);
    assert.equal(sources[0].closed,false);
    sources[0].listeners.update({data:JSON.stringify({session:{...terminal,events_captured:75,delivery_status:'delivered'},events:records.slice(50),next_after:75})});
    assert.equal(app.agentSessions.events.length,75);
    assert.equal(app.agentSessions.nextAfter,75);
    assert.equal(app.agentSessions.events.at(-1).event.type,'agent_end');
    assert.equal(app.agentSessionCaptureLabel(app.agentSessions.selected),'Transcript saved');
    assert.equal(sources[0].closed,false);
    // A terminal invocation reconnects with its delivered cursor if the browser
    // transport drops. The user closing the dialog owns stream cleanup.
    sources[0].onerror(); assert.equal(timers.length,1); timers[0]();
    assert.match(sources[1].url,/after=75$/);
    app.closeAgentSessions(); assert.equal(sources[1].closed,true);
    """)


def test_readable_activity_keeps_live_text_and_tool_output_without_repeated_end_messages():
    run_ui("""
    const app=waylineApp();
    const message=text=>({role:'assistant',content:[{type:'text',text}]});
    const records=[
      {sequence:1,event:{type:'message_start',message:message('')}},
      {sequence:2,event:{type:'message_update',message:message('Reading the')}},
      {sequence:3,event:{type:'message_update',message:message('Reading the source.')}},
      {sequence:4,event:{type:'tool_execution_start',toolCallId:'read-1',toolName:'read',args:{path:'source.txt'}}},
      {sequence:5,event:{type:'tool_execution_update',toolCallId:'read-1',toolName:'read',partialResult:{content:[{type:'text',text:'Partial source text'}]}}},
    ];
    app.mergeAgentEvents(records);
    let rows=app.agentActivityRecords();
    assert.equal(rows.length,2);
    assert.equal(app.agentEventSummary(rows[0].event),'Reading the source.');
    assert.equal(app.agentEventLabel(rows[0].event),'Assistant message fragment');
    assert.equal(app.agentEventSummary(rows[1].event),'Partial source text');
    assert.equal(app.agentEventLabel(rows[1].event),'Tool update · read');
    app.mergeAgentEvents([
      {sequence:6,event:{type:'tool_execution_end',toolCallId:'read-1',toolName:'read',isError:true,result:{content:[{type:'text',text:'Read failed'}]}}},
      {sequence:7,event:{type:'message_end',message:{role:'toolResult',content:[{type:'text',text:'Read failed'}]}}},
      {sequence:8,event:{type:'message_start',message:message('')}},
      {sequence:9,event:{type:'message_update',assistantMessageEvent:{type:'text_end',partial:message('<script>untrusted text</script>')}}},
      {sequence:10,event:{type:'message_end',message:message('<script>untrusted text</script>')}},
      {sequence:11,event:{type:'turn_end',message:message('<script>untrusted text</script>')}},
      {sequence:12,event:{type:'agent_end'}},
    ]);
    rows=app.agentActivityRecords();
    assert.equal(rows.length,4);
    assert.equal(app.agentEventLabel(rows[1].event),'Tool failed · read');
    assert.equal(app.agentEventSummary(rows[1].event),'Read failed');
    assert.equal(app.agentEventSummary(rows[2].event),'<script>untrusted text</script>');
    assert.equal(rows[2].firstSequence,9);
    assert.equal(rows[2].sequence,10);
    assert.match(app.agentActivityWindowLabel(),/Earlier or later messages may be outside/);
    const original=JSON.stringify(app.agentSessions.events);
    app.agentSessions.rawEvents=true;
    assert.equal(JSON.stringify(app.agentActivityRecords()),original,'Raw events retain their original objects and ordering');
    assert.match(app.agentActivityWindowLabel(),/Raw events shown individually/);
    """)


def test_backpaging_can_return_to_latest_without_losing_live_cursor():
    run_ui("""
    const app=waylineApp(); const paths=[];
    app.agentSessions.open=true;
    app.agentSessions.selected={id:'one',events_captured:240};
    app.agentSessions.events=Array.from({length:50},(_,i)=>({sequence:191+i,event:{type:'agent_start'}}));
    app.agentSessions.nextAfter=240;
    app.api=async(method,path)=>{
      paths.push(path);
      const after=Number(new URL('http://local'+path).searchParams.get('after'));
      return {items:Array.from({length:50},(_,i)=>({sequence:after+i+1,event:{type:'agent_start'}})),next_after:after+50};
    };
    app.connectAgentSessionStream=()=>{};
    await app.loadEarlierAgentEvents();
    await app.loadEarlierAgentEvents();
    assert.equal(app.agentSessions.events[0].sequence,91);
    assert.equal(app.agentSessions.events.at(-1).sequence,190);
    assert.equal(app.agentSessions.nextAfter,240);
    assert.match(app.agentActivityWindowLabel(),/#91–190/);
    await app.selectAgentSession(app.agentSessions.selected);
    assert.equal(app.agentSessions.events[0].sequence,191);
    assert.equal(app.agentSessions.events.at(-1).sequence,240);
    assert.equal(app.agentSessions.nextAfter,240);
    assert.match(paths.at(-1),/after=190&limit=50/);
    """)


def test_usage_gap_opens_exact_invocation_and_ignores_a_replaced_dialog():
    run_ui("""
    const app=waylineApp(); let selected;
    app.selectAgentSession=async session=>{selected=session.id;};
    app.api=async(method,path)=>path.includes('?')?{items:[],total:0}:{id:'missing-record',project_id:'project'};
    await app.openUsageSession({project_id:'project',invocation_id:'missing-record'});
    assert.equal(selected,'missing-record');
    assert.deepEqual(app.agentSessions.scope,{project_id:'project'});
    assert.match(app.agentSessions.title,/usage record missing/);
    let resolveList; selected=null;
    app.api=()=>new Promise(resolve=>resolveList=resolve);
    const opening=app.openUsageSession({project_id:'old',invocation_id:'old-session'});
    app.closeAgentSessions();
    resolveList({items:[],total:0});
    await opening;
    assert.equal(selected,null);
    assert.equal(app.agentSessions.open,false);
    assert.equal(app.workflowStatusLabel('stopped'),'stopped');
    const oldNow=Date.now; Date.now=()=>Date.parse('2026-10-28T12:00:00Z');
    assert.match(app.usageReporterAge({last_seen:'2026-10-27T12:00:00Z'}),/24 hours ago; current queue state unconfirmed/);
    assert.equal(app.usageReporterAge({last_seen:null}),'Report age unavailable');
    Date.now=oldNow;
    """)


def test_phone_stacking_preserves_saved_desktop_panel_width():
    run_ui("""
    const app=waylineApp(); let width=390;
    global.document={getElementById:()=>({getBoundingClientRect:()=>({width})})};
    global.window={innerWidth:390};
    assert.equal(app.clampPanelWidth(420),420,'Phone CSS uses full width without overwriting the desktop preference');
    width=1280;
    assert.equal(app.clampPanelWidth(420),420);
    assert.equal(app.clampPanelWidth(2000),1020,'Desktop drag still leaves room for graph');
    assert.equal(app.clampPanelWidth(100),260);
    """)


def test_capped_pi_message_exposes_recorded_error_without_inventing_budget_type():
    run_ui("""
    const app=waylineApp();
    const event={type:'message_end',message:{role:'assistant',content:[],stopReason:'error',errorMessage:'benchmark provider-request budget is stopped'}};
    app.mergeAgentEvents([{sequence:95,event},{sequence:96,event:{type:'turn_end',message:event.message}},{sequence:97,event:{type:'agent_end'}}]);
    const rows=app.agentActivityRecords();
    assert.equal(rows.length,2);
    assert.equal(app.agentEventLabel(rows[0].event),'Assistant error');
    assert.equal(app.agentEventSummary(rows[0].event),'benchmark provider-request budget is stopped');
    """)


def test_missing_report_card_counts_disjoint_missing_rows_and_unreported_records():
    run_ui("""
    const app=waylineApp();
    app.usageSummary={totals:{unreported_attempts:0},session_coverage:{missing_usage_attempts:1}};
    assert.equal(app.usageMissingReportCount(),1);
    app.usageSummary={totals:{unreported_attempts:1},session_coverage:{missing_usage_attempts:0}};
    assert.equal(app.usageMissingReportCount(),1,'A late usage row replaces the missing-row gap without double counting');
    app.usageSummary={totals:{unreported_attempts:2},session_coverage:{missing_usage_attempts:3}};
    assert.equal(app.usageMissingReportCount(),5);
    app.usageSummary={totals:{unreported_attempts:0}};
    assert.equal(app.usageMissingReportCount(),0);
    """)


def test_initial_graph_fit_cannot_animate_back_to_pre_phone_layout_dimensions():
    run_ui("""
    const app=waylineApp(); let options;
    global.document={getElementById:()=>({})};
    global.cytoscape=opts=>{options=opts;return {on:()=>{}};};
    app.buildElements=()=>({nodes:[],edges:[]}); app.graphStyles=()=>[];
    app.pulseActive=()=>{}; app.setupAutoFit=()=>{};
    app.initGraph();
    assert.equal(options.layout.animate,false,'Initial layout cannot leave a stale fit animation running after final responsive resize');
    assert.equal(app.layoutOpts().animate,true,'Explicit layout changes retain their animation');
    """)


def test_resize_fit_preserves_camera_for_same_size_and_cannot_reach_replacement_graph():
    run_ui("""
    const app=waylineApp(); const timers=[]; const observers=[];
    let size={width:390,height:373};
    const container={getBoundingClientRect:()=>size};
    global.document={getElementById:()=>container};
    global.ResizeObserver=class {constructor(fn){this.update=fn;observers.push(this);}observe(){}disconnect(){this.disconnected=true;}};
    global.setTimeout=fn=>{timers.push(fn);return timers.length;}; global.clearTimeout=()=>{};
    let firstFits=0,secondFits=0;
    const first={resize:()=>{},fit:()=>firstFits++};
    const second={resize:()=>{},fit:()=>secondFits++};
    app.clampPanelWidth=width=>width;
    app.cy=first; app.setupAutoFit(); observers[0].update(); timers[0]();
    assert.equal(firstFits,1);
    // The user can now pan/zoom. Repeated same-size notifications must not fit.
    observers[0].update(); assert.equal(timers.length,1);
    size={width:390,height:410}; observers[0].update();
    app.teardownAutoFit(); app.cy=second;
    timers[1]();
    assert.equal(secondFits,0,'A pending fit from the closed graph cannot change its replacement');
    assert.equal(firstFits,1);
    assert.equal(observers[0].disconnected,true);
    app.setupAutoFit(); observers[1].update(); timers[2]();
    assert.equal(secondFits,1,'The replacement graph gets its own fit at its final dimensions');
    """)
