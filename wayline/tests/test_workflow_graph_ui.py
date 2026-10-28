"""Workflow graph projections from real API records; deterministic, no model calls."""
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from wayline.server import db
from wayline.server.app import app
from test_agent_session_ui import run_ui


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "workflow-graph-fixture.db")
    with TestClient(app) as client:
        yield client


def create(client, stages):
    response = client.post("/workflows", json={
        "title": "Deterministic UI fixture", "origin": "No model calls. Recorded outcomes below are test fixtures.",
        "stages": stages,
    })
    assert response.status_code == 201, response.text
    return response.json()


def stage(id, **extra):
    return {"id": id, "title": id.title(), "goal": "Fixture goal " + id, **extra}


def complete(client, project, **extra):
    response = client.post(f"/projects/{project}/complete", json={
        "from": ["origin"], "worker": "ui-fixture", "description": "Deterministic fixture completion, not model work.", **extra,
    })
    assert response.status_code == 200, response.text
    return response.json()


def detail(client, workflow):
    return client.get("/workflows/" + workflow["workflow"]["id"]).json()


def js_workflow(workflow):
    return "const app=waylineApp();app.workflow=" + json.dumps(workflow) + ";"


def test_all_expansion_uses_actual_server_join_dependencies(client):
    workflow = create(client, [stage("find", expansion={"mode": "all"}), stage("join", depends_on=["find"])])
    complete(client, workflow["stages"][0]["project_id"], goals=[stage("alpha"), stage("beta")])
    current = detail(client, workflow)
    run_ui(js_workflow(current) + """
    const original=JSON.stringify(app.workflow);
    const graph=app.workflowGraphElements();
    assert.equal(graph.nodes.length,4);
    const join=graph.edges.filter(edge=>edge.data.target==='stage:join');
    assert.deepEqual(join.map(edge=>edge.data.source).sort(),['stage:alpha','stage:beta','stage:find']);
    assert.equal(graph.nodes.find(node=>node.data.id==='stage:alpha').data.state,'active');
    assert.equal(app.workflowStageGraphStatus(app.workflow.stages.find(stage=>stage.id==='join')),'Waiting for prerequisites');
    assert.ok(!JSON.stringify(graph).includes('Running'));
    assert.equal(JSON.stringify(app.workflow),original,'Projection cannot rewrite execution prerequisites');
    """)


def test_choice_fallback_has_one_selected_join_and_retains_other_options(client):
    workflow = create(client, [stage("choose", expansion={"mode": "one"}), stage("serve", depends_on=["choose"])])
    complete(client, workflow["stages"][0]["project_id"], goals=[stage("alpha"), stage("beta"), stage("gamma")],
             selected_goal="alpha", selection_reason="Fixture ranking")
    first = next(item for item in detail(client, workflow)["stages"] if item["id"] == "alpha")
    response = client.post(f"/projects/{first['project_id']}/unavailable", json={
        "from": ["origin"], "worker": "ui-fixture", "description": "Fixture ingredient unavailable",
    })
    assert response.status_code == 200
    current = detail(client, workflow)
    run_ui(js_workflow(current) + """
    const original=JSON.stringify(app.workflow);
    const graph=app.workflowGraphElements();
    const selected=graph.edges.filter(edge=>edge.data.id.startsWith('choice-selected:'));
    assert.equal(selected.length,1);
    assert.equal(selected[0].data.source,'stage:beta');
    assert.equal(selected[0].data.target,'choice:choose');
    assert.ok(!graph.edges.some(edge=>edge.data.id==='choice-proposal:choose'),'No shortcut may cross an unselected option into the gate');
    assert.equal(graph.edges.find(edge=>edge.data.id==='dependency:serve:0').data.source,'choice:choose');
    assert.ok(!graph.edges.some(edge=>['stage:alpha','stage:gamma'].includes(edge.data.source)&&edge.data.target==='choice:choose'));
    assert.equal(graph.nodes.find(node=>node.data.id==='stage:gamma').data.state,'pending');
    app.selectWorkflowGraphNode('stage:beta');
    assert.deepEqual(app.workflowInspectorStages().map(stage=>stage.id),['beta']);
    app.selectWorkflowRecord('edge','dependency:serve:0');
    assert.match(app.workflowSelectedGraphEdge().explanation,/Unselected, skipped and unavailable alternatives are not required/);
    assert.equal(JSON.stringify(app.workflow),original);
    """)


def test_collections_and_false_condition_keep_gate_and_artifact_meanings(client):
    count = {"count": {"type": "integer", "minimum": 0}}
    workflow = create(client, [
        stage("discover", expansion={"mode": "all"}, emits={"collection": "checks", "result": count}),
        stage("scope", result=count),
        stage("optional", depends_on=[{"stage": "scope", "when": "scope.result.count > 0"}]),
        stage("finish", depends_on=[{"collection": "checks"}, {"stage": "optional", "allow_not_applicable": True}]),
    ])
    projects = {item["id"]: item["project_id"] for item in workflow["stages"]}
    complete(client, projects["discover"], goals=[stage("alpha", artifacts=[{"path": "alpha.json"}]), stage("beta")])
    complete(client, projects["scope"], result={"count": 0})
    current = detail(client, workflow)
    alpha = next(item for item in current["stages"] if item["id"] == "alpha")
    artifact = client.post(f"/projects/{alpha['project_id']}/artifacts", params={"path": "alpha.json"}, content=b'{"count":1}').json()
    complete(client, alpha["project_id"], result={"count": 1})
    current = detail(client, workflow)
    run_ui(js_workflow(current) + """
    const graph=app.workflowGraphElements();
    const collection=graph.nodes.find(node=>node.data.id==='collection:checks');
    assert.equal(collection.data.state,'waiting');
    assert.match(collection.data.label,/2 members/);
    assert.equal(graph.edges.find(edge=>edge.data.id==='dependency:finish:0').data.source,'collection:checks');
    assert.equal(graph.edges.filter(edge=>edge.data.edgeType==='membership').length,2);
    assert.ok(!graph.edges.some(edge=>edge.data.id==='collection-emitter:checks'),'Discovery already reaches the collection through actual members');
    const condition=graph.edges.find(edge=>edge.data.id==='dependency:optional:0').data;
    assert.equal(condition.condition,'scope.result.count > 0');
    assert.equal(condition.reason,'');
    assert.match(condition.destinationOutcome,/Condition is false/);
    assert.equal(graph.nodes.find(node=>node.data.id==='stage:optional').data.state,'not_applicable');
    assert.equal(graph.edges.find(edge=>edge.data.id==='dependency:finish:1').data.allowAbsent,true);
    assert.equal(graph.edges.find(edge=>edge.data.id==='dependency:finish:1').data.label,'Absence allowed');
    app.selectWorkflowGraphNode('collection:checks');
    assert.equal(app.workflowInspectorCollections()[0].members[0].artifacts.length,1);
    assert.equal(app.workflowOutputArtifacts()[0].artifact.id,""" + json.dumps(artifact["id"]) + """);
    """)
    beta = next(item for item in current["stages"] if item["id"] == "beta")
    complete(client, beta["project_id"], result={"count": 1})
    run_ui(js_workflow(detail(client, workflow)) + """
    assert.equal(app.workflowGraphElements().nodes.find(node=>node.data.id==='collection:checks').data.state,'completed');
    assert.equal(app.workflow.stages.find(stage=>stage.id==='finish').status,'active');
    """)


def test_empty_collection_is_ready_and_review_is_not_completion(client):
    workflow = create(client, [stage("find", expansion={"mode": "all"}, emits={"collection": "items"}),
                               stage("use", depends_on=[{"collection": "items"}], verification_enabled=True)])
    complete(client, workflow["stages"][0]["project_id"], goals=[])
    current = detail(client, workflow)
    use = next(item for item in current["stages"] if item["id"] == "use")
    complete(client, use["project_id"], producer_agent_id="fixture-producer", producer_session_id="fixture-session")
    run_ui(js_workflow(detail(client, workflow)) + """
    const graph=app.workflowGraphElements();
    assert.match(graph.nodes.find(node=>node.data.id==='collection:items').data.label,/Ready\\n0 members/);
    assert.equal(graph.nodes.find(node=>node.data.id==='stage:use').data.state,'review');
    assert.equal(app.workflow.stages.find(stage=>stage.id==='use').completion,null);
    app.selectWorkflowGraphNode('stage:use');
    let path;app.api=async(method,url)=>{path=url;return {status:'pending'};};
    await app.loadWorkflowStageReview(app.workflowInspectorStages()[0]);
    assert.equal(path,'/reviews/'+app.workflowInspectorStages()[0].pending_review_id);
    """)


def test_polling_updates_and_new_branches_preserve_real_cytoscape_camera_and_positions():
    cytoscape = Path(__file__).parents[1] / "src/wayline/server/static/vendor/cytoscape.min.js"
    run_ui("const cytoscape=require(" + json.dumps(str(cytoscape)) + ");" + """
    const app=waylineApp();app.view='workflow';
    app.workflow={workflow:{id:'one',status:'active'},stages:[{id:'find',title:'Find',status:'active',depends_on:[]}],collections:[]};
    const data=app.workflowGraphElements();
    app.workflowCy=cytoscape({headless:true,elements:data.nodes,layout:{name:'preset'}});
    const graph=app.workflowCy;graph.getElementById('stage:find').position({x:50,y:80});
    graph.pan({x:123,y:234});graph.zoom(1.7);
    app.workflowSelectedRecord={type:'stage',id:'find'};
    const position={...graph.getElementById('stage:find').position()};const pan={...graph.pan()};const zoom=graph.zoom();
    app.workflow.stages[0].status='completed';app.updateWorkflowGraph();
    assert.deepEqual(graph.pan(),pan);assert.equal(graph.zoom(),zoom);
    app.workflow.stages.push({id:'child',title:'Child',status:'active',depends_on:['find'],generated_by_stage_id:'find'});
    app.updateWorkflowGraph();
    assert.deepEqual(graph.getElementById('stage:find').position(),position);
    assert.deepEqual(graph.pan(),pan);assert.equal(graph.zoom(),zoom);
    assert.equal(graph.getElementById('stage:child').length,1);
    assert.notDeepEqual(graph.getElementById('stage:child').position(),position);
    assert.equal(app.workflowTopologyChanged,true);
    assert.deepEqual(app.workflowSelectedRecord,{type:'stage',id:'find'});
    app.disposeWorkflowGraph();assert.equal(graph.destroyed(),true);
    """)



def test_switching_workflows_preserves_destination_selection_without_updating_origin_graph(client):
    first = detail(client, create(client, [stage("only_a")]))
    second = detail(client, create(client, [stage("only_b")]))
    cytoscape = Path(__file__).parents[1] / "src/wayline/server/static/vendor/cytoscape.min.js"
    run_ui("const realCy=require(" + json.dumps(str(cytoscape)) + ");" + js_workflow(first)
           + "const destination=" + json.dumps(second) + ";" + """
    app.view='workflow';app.selectedWorkflowId=app.workflow.workflow.id;
    app.$refs={workflowScroll:{scrollTop:84}};app.$nextTick=fn=>fn();
    global.location={hash:'#/workflows/'+app.selectedWorkflowId};
    const container={addEventListener(){},removeEventListener(){}};
    global.document={getElementById:()=>container};
    global.ResizeObserver=class {observe(){} disconnect(){}};
    global.cytoscape=options=>realCy({...options,container:undefined,headless:true,layout:{name:'preset'}});
    app.initWorkflowGraph();
    app.selectWorkflowRecord('stage','only_a');
    const originGraph=app.workflowCy;
    const originElements=originGraph.elements().map(element=>element.id()).sort();
    const destinationId=destination.workflow.id;
    app.workflowRecordSelections[destinationId]={type:'stage',id:'only_b'};
    app.api=async()=>destination;
    const dispose=app.disposeWorkflowGraph.bind(app);
    let originChecked=false;
    app.disposeWorkflowGraph=()=>{
      assert.deepEqual(originGraph.elements().map(element=>element.id()).sort(),originElements,
        'The still-mounted origin graph must not receive destination nodes before disposal');
      assert.deepEqual(app.workflowRecordSelections[destinationId],{type:'stage',id:'only_b'});
      originChecked=true;dispose();
    };
    await app.openWorkflow(destinationId);
    assert.equal(originChecked,true);assert.equal(originGraph.destroyed(),true);
    assert.deepEqual(app.workflowSelectedRecord,{type:'stage',id:'only_b'});
    assert.deepEqual(app.workflowRecordSelections[destinationId],{type:'stage',id:'only_b'});
    assert.equal(app.workflowCy.getElementById('stage:only_a').length,0);
    assert.equal(app.workflowCy.getElementById('stage:only_b').length,1);
    app.disposeWorkflowGraph=dispose;app.disposeWorkflowGraph();
    """)


def test_stale_workflow_responses_cannot_replace_newer_graph_data():
    run_ui("""
    const app=waylineApp();const pending=[];
    app.selectedWorkflowId='one';app.api=()=>new Promise(resolve=>pending.push(resolve));
    const first=app.loadWorkflow('one');const second=app.loadWorkflow('one');
    pending[1]({workflow:{id:'one',title:'Newer'},stages:[]});await second;
    pending[0]({workflow:{id:'one',title:'Older'},stages:[]});await first;
    assert.equal(app.workflow.workflow.title,'Newer');
    app.selectedWorkflowId='two';
    const old=app.loadWorkflow('one');pending[2]({workflow:{id:'one'},stages:[]});
    assert.equal(await old,null);
    assert.equal(app.workflow.workflow.title,'Newer');
    """)


def test_not_applicable_choice_allows_only_authored_absence_gates(client):
    workflow = create(client, [
        stage("scope", result={"count": {"type": "integer"}}),
        stage("choose", depends_on=[{"stage": "scope", "when": "scope.result.count > 0"}], expansion={"mode": "one"}),
        stage("partial", depends_on=[{"stage": "choose", "allow_not_applicable": True}]),
        stage("required", depends_on=["choose"]),
    ])
    complete(client, workflow["stages"][0]["project_id"], result={"count": 0})
    run_ui(js_workflow(detail(client, workflow)) + """
    const graph=app.workflowGraphElements();
    const gate=graph.nodes.find(node=>node.data.id==='choice:choose').data;
    assert.equal(gate.state,'not_applicable');
    assert.match(gate.label,/Not applicable/);
    assert.ok(!gate.label.includes('must succeed'));
    const partial=graph.edges.find(edge=>edge.data.id==='dependency:partial:0').data;
    assert.equal(partial.allowAbsent,true);
    assert.equal(partial.label,'Absence allowed');
    assert.match(partial.explanation,/no selected option is required/);
    assert.equal(app.workflow.stages.find(stage=>stage.id==='partial').status,'active');
    assert.equal(app.workflow.stages.find(stage=>stage.id==='required').status,'not_applicable');
    assert.equal(graph.edges.filter(edge=>edge.data.id.startsWith('choice-selected:')).length,0);
    """)


def test_workflow_resize_refits_after_node_tap_and_preserves_manual_world_center():
    cytoscape = Path(__file__).parents[1] / "src/wayline/server/static/vendor/cytoscape.min.js"
    run_ui("const realCy=require(" + json.dumps(str(cytoscape)) + ");" + """
    const app=waylineApp();app.view='workflow';app.selectedWorkflowId='one';
    app.workflow={workflow:{id:'one',status:'completed'},stages:[{id:'a',title:'First goal',status:'completed',depends_on:[]},{id:'b',title:'Second goal',status:'completed',depends_on:['a']}],collections:[]};
    const timers=[],observers=[],graphs=[];let size={width:840,height:800};
    global.document={getElementById:()=>({getBoundingClientRect:()=>size})};
    const nativeTimeout=global.setTimeout;
    global.setTimeout=(fn,delay,...args)=>{if(delay===80){timers.push(fn);return 0;}return nativeTimeout(fn,delay,...args);};
    global.ResizeObserver=class {constructor(fn){this.update=fn;observers.push(this);}observe(){}disconnect(){this.disconnected=true;}};
    global.cytoscape=options=>{const graph=realCy({...options,container:undefined,headless:true,styleEnabled:true,layout:{name:'preset'}});graphs.push(graph);return graph;};
    app.initWorkflowGraph();const graph=app.workflowCy;
    graph.getElementById('stage:a').position({x:120,y:80});graph.getElementById('stage:b').position({x:120,y:240});
    const checkCentered=()=>{
      const bounds=graph.elements().boundingBox(), pan=graph.pan(),zoom=graph.zoom();
      assert.ok(Math.abs((bounds.x1+bounds.x2)/2*zoom+pan.x-size.width/2)<0.01);
      assert.ok(Math.abs((bounds.y1+bounds.y2)/2*zoom+pan.y-size.height/2)<0.01);
      assert.ok(zoom<=1.05,'Small graphs cannot grow into giant nodes');
    };
    observers[0].update();timers[0]();checkCentered();
    observers[0].update();assert.equal(timers.length,1,'Repeated dimensions do not move the camera');
    graph.getElementById('stage:a').emit('tap');
    assert.equal(app.workflowInspectorOpen,true);assert.equal(app.workflowCameraTouched,false,'Node selection is not camera manipulation');
    size={width:720,height:1000};observers[0].update();timers[1]();checkCentered();
    size={width:390,height:760};observers[0].update();timers[2]();checkCentered();
    graph.zoom(1.6);graph.pan({x:112,y:224});graph.emit('dragpan');
    const before={x:(size.width/2-graph.pan('x'))/graph.zoom(),y:(size.height/2-graph.pan('y'))/graph.zoom()};
    size={width:1280,height:660};observers[0].update();timers[3]();
    assert.equal(graph.zoom(),1.6,'Manual zoom is retained');
    assert.ok(Math.abs((size.width/2-graph.pan('x'))/graph.zoom()-before.x)<0.01);
    assert.ok(Math.abs((size.height/2-graph.pan('y'))/graph.zoom()-before.y)<0.01);
    size={width:920,height:660};observers[0].update();timers[4]();
    assert.equal(graph.zoom(),1.6,'Docking details preserves a manually chosen camera');
    size={width:900,height:500};observers[0].update();app.disposeWorkflowGraph();
    assert.equal(graph.destroyed(),true);assert.equal(observers[0].disconnected,true);
    app.initWorkflowGraph();const replacement=app.workflowCy;
    timers[5]();assert.equal(app._workflowViewportSize,null,'Stale observer cannot move the replacement graph');
    app.disposeWorkflowGraph();assert.equal(replacement.destroyed(),true);
    """)


def test_workflow_inspector_is_on_demand_closable_and_keeps_selection():
    run_ui("""
    const app=waylineApp();app.selectedWorkflowId='one';app.workflowMenuOpen=true;
    app.workflow={workflow:{id:'one'},stages:[{id:'goal',title:'Goal',status:'active',depends_on:[]}],collections:[]};
    let focused='',returnFocused=0;
    const origin={isConnected:true,focus(){returnFocused++;}};
    const close={focus(){focused='close';},getClientRects:()=>[{}]};
    const last={focus(){focused='last';},getClientRects:()=>[{}]};
    global.document={activeElement:origin};app.$nextTick=fn=>fn();
    app.$refs={workflowInspectorClose:close,workflowScroll:{scrollTop:12,querySelectorAll:()=>[close,last]}};
    assert.equal(app.workflowInspectorOpen,false);
    app.selectWorkflowRecord('stage','goal');
    assert.equal(app.workflowInspectorOpen,true);assert.equal(app.workflowMenuOpen,false);assert.equal(focused,'close');
    app.workflowInspectorOverlay=true;document.activeElement=last;let prevented=0;
    app.workflowInspectorKeydown({key:'Tab',shiftKey:false,preventDefault(){prevented++;}});
    assert.equal(focused,'close');assert.equal(prevented,1);
    document.activeElement=close;
    app.workflowInspectorKeydown({key:'Tab',shiftKey:true,preventDefault(){prevented++;}});
    assert.equal(focused,'last');assert.equal(prevented,2);
    app.closeWorkflowInspector();assert.equal(app.workflowInspectorOpen,false);assert.equal(returnFocused,1);
    assert.deepEqual(app.workflowSelectedRecord,{type:'stage',id:'goal'});
    assert.deepEqual(app.workflowRecordSelections.one,{type:'stage',id:'goal'});
    app.ensureWorkflowSelection({nodes:[],edges:[]});
    assert.equal(app.workflowInspectorOpen,false,'An invalidated record cannot open a hidden drawer');
    app.selectWorkflowRecord('overview','overview');assert.equal(app.workflowInspectorOpen,true);
    """)


def test_exhausted_choice_retains_blocked_gate_without_unselected_contributors(client):
    workflow = create(client, [stage("choose", expansion={"mode": "one"}), stage("serve", depends_on=["choose"])])
    complete(client, workflow["stages"][0]["project_id"], goals=[stage("alpha"), stage("beta")],
             selected_goal="alpha", selection_reason="Fixture ranking")
    for name in ("alpha", "beta"):
        option = next(item for item in detail(client, workflow)["stages"] if item["id"] == name)
        response = client.post(f"/projects/{option['project_id']}/unavailable", json={
            "from": ["origin"], "worker": "ui-fixture", "description": "Fixture alternative unavailable",
        })
        assert response.status_code == 200
    run_ui(js_workflow(detail(client, workflow)) + """
    const graph=app.workflowGraphElements();
    assert.equal(graph.nodes.find(node=>node.data.id==='choice:choose').data.state,'blocked');
    assert.equal(graph.edges.filter(edge=>edge.data.id.startsWith('choice-selected:')).length,0);
    const recovery=graph.edges.find(edge=>edge.data.id==='choice-proposal:choose').data;
    assert.equal(recovery.source,'stage:choose');assert.equal(recovery.target,'choice:choose');
    assert.equal(recovery.edgeType,'choice-recovery');assert.ok(recovery.routeOffset>=300);
    const layout=app.workflowLayoutOptions();
    assert.equal(layout.minLen({data:key=>recovery[key]}),2);
    assert.equal(layout.minLen({data:()=>'dependency'}),1);
    assert.match(recovery.explanation,/unavailable options do not satisfy/);
    assert.equal(graph.edges.find(edge=>edge.data.id==='dependency:serve:0').data.source,'choice:choose');
    assert.ok(app.workflow.stages.find(stage=>stage.id==='choose').choice_error);
    app.workflowSelectedRecord={type:'edge',id:'choice-selected:choose:alpha'};
    app.ensureWorkflowSelection(graph);
    assert.deepEqual(app.workflowSelectedRecord,{type:'overview',id:'overview'});
    """)


def test_pause_response_updates_graph_before_poll_and_invalidates_older_read():
    run_ui("""
    const app=waylineApp();app.view='workflow';app.selectedWorkflowId='one';app.workflowCy={};
    app.workflow={workflow:{id:'one',status:'active'},stages:[]};
    let updates=0;app.updateWorkflowGraph=()=>updates++;
    app.loadProjects=async()=>{};app.loadWorkflows=async()=>{};app.showToast=()=>{};
    let resolvePoll;
    app.api=(method)=>method==='GET'?new Promise(resolve=>resolvePoll=resolve):Promise.resolve({workflow:{id:'one',status:'stopped'},stages:[]});
    const poll=app.loadWorkflow('one');
    await app.changeWorkflowStatus('one','active');
    assert.equal(updates,1);assert.equal(app.workflow.workflow.status,'stopped');
    resolvePoll({workflow:{id:'one',status:'active'},stages:[]});await poll;
    assert.equal(updates,1);assert.equal(app.workflow.workflow.status,'stopped');
    """)


def test_failed_workflow_navigation_preserves_project_poll_stop_and_scope():
    run_ui("""
    const app=waylineApp();app.view='graph';app.selectedProjectId='project-one';
    app.project={project:{id:'project-one',status:'active'}};
    const graph={destroy(){throw Error('Failed navigation cannot destroy origin graph');}};app.cy=graph;
    app.showToast=()=>{};app.api=async()=>{throw Error('404 missing workflow');};
    await app.openWorkflow('missing');
    assert.equal(app.view,'graph');assert.equal(app.selectedProjectId,'project-one');
    assert.equal(app.selectedWorkflowId,'');assert.equal(app.cy,graph);
    let poll;global.setInterval=fn=>{poll=fn;return 1;};let refreshed;
    app.loadProject=async id=>refreshed=id;app.updateGraph=()=>{};app.startPolling();await poll();
    assert.equal(refreshed,'project-one','Project polling still targets the origin project');
    let stopped;app.setProjectStatus=async(id,status)=>stopped={id,status};await app.toggleProjectStop();
    assert.deepEqual(stopped,{id:'project-one',status:'stopped'});
    assert.equal(app.project.project.id,app.selectedProjectId,'Session scope still has its project id');
    """)


def test_project_navigation_saves_inspector_scroll_before_clearing_workflow_id():
    run_ui("""
    const app=waylineApp();app.view='workflow';app.selectedWorkflowId='one';
    app.$refs={workflowScroll:{scrollTop:584}};app.$nextTick=fn=>fn();
    global.location={hash:'#/workflows/one'};
    app.rememberProjectListScroll=()=>{};app.clampPanelWidth=width=>width;
    app.initGraph=()=>{};app.initWorkflowGraph=()=>{};
    app.loadProject=async id=>{app.project={project:{id,workflow_id:'one'}};};
    await app.openProject('stage-project');
    assert.equal(app.workflowScrollPositions.one,584);
    assert.equal(app.selectedWorkflowId,'');assert.equal(app.view,'graph');
    app.$refs.workflowScroll.scrollTop=0;
    app.api=async()=>({workflow:{id:'one',status:'active'},stages:[],collections:[]});
    await app.backFromProject();
    // backFromProject starts the async open, so await its shared request/continuation.
    await Promise.resolve();await Promise.resolve();
    assert.equal(app.view,'workflow');assert.equal(app.$refs.workflowScroll.scrollTop,584);
    """)


@pytest.mark.parametrize("destination", ["one", "two"])
def test_workflow_poll_shares_open_request_and_navigation_initializes_once(destination):
    run_ui("const destination=" + json.dumps(destination) + ";" + """
    const app=waylineApp();app.view='workflow';app.selectedWorkflowId='one';
    app.workflow={workflow:{id:'one',status:'active'},stages:[]};
    app.$refs={workflowScroll:{scrollTop:28}};app.$nextTick=fn=>fn();
    global.location={hash:'#/workflows/one'};
    let requests=0,resolveRequest,destroyed=0,initialized=0,poll;
    app.workflowCy={destroy(){destroyed++;}};
    app.updateWorkflowGraph=()=>{};app.initWorkflowGraph=()=>initialized++;
    app.api=()=>{requests++;return new Promise(resolve=>resolveRequest=resolve);};
    global.setInterval=fn=>{poll=fn;return 1;};app.startPolling();
    const opening=app.openWorkflow(destination);
    const polling=poll();
    assert.equal(requests,1,'A poll cannot supersede the in-flight navigation request');
    assert.equal(destroyed,0,'Origin graph stays usable while navigation loads');
    resolveRequest({workflow:{id:destination,status:'active'},stages:[],collections:[]});
    await Promise.all([opening,polling]);
    assert.equal(app.view,'workflow');assert.equal(app.workflow.workflow.id,destination);
    assert.equal(app.selectedWorkflowId,destination);assert.equal(initialized,1);
    assert.equal(destroyed,1);assert.equal(app.polling,true);assert.equal(app.replay.active,false);
    assert.equal(location.hash.replace(/^#/,''),'/workflows/'+destination);
    assert.equal(app.workflowOpeningLoad,null);
    """)


def test_superseded_same_workflow_navigation_cannot_restore_old_selection():
    run_ui("""
    const app=waylineApp();app.view='workflow';app.selectedWorkflowId='one';
    app.$refs={};app.$nextTick=fn=>fn();global.location={hash:'#/workflows/one'};
    app.initWorkflowGraph=()=>{};const responses=[];
    app.api=()=>new Promise(resolve=>responses.push(resolve));
    const first=app.openWorkflow('two');const second=app.openWorkflow('two');
    responses[0]({workflow:{id:'two',status:'active'},stages:[]});await first;
    assert.equal(app.selectedWorkflowId,'two');
    responses[1]({workflow:{id:'two',status:'active'},stages:[]});await second;
    assert.equal(app.view,'workflow');assert.equal(app.workflow.workflow.id,'two');
    """)


def test_destination_outcome_is_not_attributed_to_each_incoming_edge(client):
    count = {"count": {"type": "integer"}}
    workflow = create(client, [stage("good"), stage("scope", result=count),
                               stage("use", depends_on=["good", {"stage": "scope", "when": "scope.result.count > 0"}])])
    projects = {item["id"]: item["project_id"] for item in workflow["stages"]}
    complete(client, projects["good"])
    complete(client, projects["scope"], result={"count": 0})
    run_ui(js_workflow(detail(client, workflow)) + """
    const edges=app.workflowGraphElements().edges.filter(edge=>edge.data.target==='stage:use');
    const good=edges.find(edge=>edge.data.source==='stage:good').data;
    const conditional=edges.find(edge=>edge.data.source==='stage:scope').data;
    assert.equal(good.condition,'');assert.equal(good.reason,'');
    assert.equal(conditional.condition,'scope.result.count > 0');assert.equal(conditional.reason,'');
    assert.match(good.destinationOutcome,/scope.result.count > 0/);
    assert.equal(good.destinationOutcome,conditional.destinationOutcome,'It is a destination outcome, not per-edge causal evidence');
    """)


def test_retried_selected_option_is_paused_until_resume_not_a_recovery_block(client):
    workflow = create(client, [stage("choose", expansion={"mode": "one"}), stage("serve", depends_on=["choose"])])
    complete(client, workflow["stages"][0]["project_id"], goals=[stage("alpha")], selected_goal="alpha", selection_reason="Fixture ranking")
    option = next(item for item in detail(client, workflow)["stages"] if item["id"] == "alpha")
    assert client.post(f"/projects/{option['project_id']}/unavailable", json={"from": ["origin"], "worker": "ui-fixture", "description": "Fixture option unavailable"}).status_code == 200
    response = client.post(f"/workflows/{workflow['workflow']['id']}/choices/choose/retry", json={"goal_id": "alpha", "reason": "Fixture ingredients now available"})
    assert response.status_code == 200, response.text
    run_ui(js_workflow(response.json()) + """
    const option=app.workflow.stages.find(stage=>stage.id==='alpha');
    assert.equal(option.option_state,'selected');assert.equal(option.status,'blocked');
    assert.equal(app.workflowStageGraphState(option),'paused');
    assert.equal(app.workflowStageGraphStatus(option),'Paused · selected');
    // The same admitted-ready shape in an active workflow waits for admission, not repair.
    app.workflow.workflow.status='active';
    assert.equal(app.workflowStageGraphState(option),'ready');
    assert.equal(app.workflowStageGraphStatus(option),'Ready · awaiting admission · selected');
    option.result_error='Fixture result requires correction';
    assert.equal(app.workflowStageGraphState(option),'blocked');
    """)


def test_inline_review_read_failure_can_retry_and_preserves_full_read_only_evidence():
    run_ui("""
    const app=waylineApp();const stage={pending_review_id:'pending-claim'};
    app.api=async()=>{throw Error('Temporary connection failure');};
    await app.loadWorkflowStageReview(stage);
    assert.match(app.workflowReviewErrors['pending-claim'],/Temporary connection failure/);
    assert.equal(app.workflowReviewLoading['pending-claim'],false);
    assert.equal(app.reviewDetails['pending-claim'],undefined);
    const payload={status:'pending',completion:{description:'Unapproved fixture claim'},evidence:['Fixture source A'],snapshot:{facts:[{id:'f1',description:'Recorded fixture evidence'}]}};
    let method,path;app.api=async(m,p)=>{method=m;path=p;return payload;};
    await app.loadWorkflowStageReview(stage);
    assert.equal(method,'GET');assert.equal(path,'/reviews/pending-claim');
    assert.equal(app.workflowReviewErrors['pending-claim'],'');
    assert.deepEqual(app.reviewDetails['pending-claim'],payload);
    assert.equal(app.reviewDetails['pending-claim'].status,'pending');
    await app.loadWorkflowStageReview(stage);
    assert.equal(app.reviewDetails['pending-claim'],undefined,'Evidence panel can close without changing review state');
    """)


def test_escape_dispatch_keeps_underlying_inspector_when_other_dialog_closes_first():
    # Exercise the actual Alpine directive bodies with real dispatched key events.
    # EventTarget supplies listener ordering/target; closest supplies dialog ancestry.
    import re
    from test_agent_session_ui import HTML

    html = HTML.read_text()
    workflow_escape = re.search(r'@keydown.escape.window="([^"]+)"', html[html.index('<!-- Workflow view -->'):]).group(1)
    sessions_escape = re.search(r'@keydown.escape.window="([^"]+)"', html[html.index('<div x-show="agentSessions.open"'):]).group(1)
    retry_escape = re.search(r'@keydown.escape.stop="([^"]+)"', html[html.index('<!-- Retry an exhausted choice -->'):]).group(1)
    run_ui("const directives=" + json.dumps([workflow_escape, sessions_escape, retry_escape]) + ";" + """
    const app=waylineApp();app.view='workflow';app.workflowInspectorOpen=true;
    const [workflowEscape,sessionsEscape,retryEscape]=directives.map(source=>new Function('$event','with(this){'+source+'}'));
    const inspector={},sessions={},retry={};let focus='retry reason';
    const trigger={isConnected:true,focus(){focus='retry trigger';}};
    app.$nextTick=fn=>fn();
    app.$refs={workflowScroll:inspector,workflowMenuButton:{focus(){focus='menu';}},workflowInspectorClose:{focus(){focus='inspector close';}}};
    const key=()=>{const event=new Event('keydown',{bubbles:true,cancelable:true});Object.defineProperty(event,'key',{value:'Escape'});return event;};
    const inside=dialog=>{const target=new EventTarget();target.closest=selector=>{assert.equal(selector,'[role="dialog"][aria-modal="true"]');return dialog;};return target;};
    const sessionTarget=inside(sessions);app.agentSessions.open=true;
    sessionTarget.addEventListener('keydown',event=>sessionsEscape.call(app,event));
    let laterWorkflowCalls=0;
    sessionTarget.addEventListener('keydown',event=>{laterWorkflowCalls++;workflowEscape.call(app,event);});
    sessionTarget.dispatchEvent(key());
    assert.equal(laterWorkflowCalls,0,'The Sessions listener consumes a handled Escape before later window listeners');
    const closedSessionTarget=inside(sessions);
    closedSessionTarget.addEventListener('keydown',event=>workflowEscape.call(app,event));closedSessionTarget.dispatchEvent(key());
    assert.equal(app.agentSessions.open,false);assert.equal(app.workflowInspectorOpen,true,'The later workflow listener must use the event origin, not the now-closed Sessions flag');
    const headerTarget=inside(null);
    headerTarget.addEventListener('keydown',event=>workflowEscape.call(app,event));
    app.showWorkflowDeleteModal=true;headerTarget.dispatchEvent(key());
    assert.equal(app.workflowInspectorOpen,true,'An unfocused Delete dialog still protects the inspector');app.showWorkflowDeleteModal=false;
    global.document={querySelectorAll:()=>[{getClientRects:()=>[{}]}]};headerTarget.dispatchEvent(key());
    assert.equal(app.workflowInspectorOpen,true,'Other visible modal dialogs protect the inspector even when the opener keeps focus');document.querySelectorAll=()=>[];
    app.workflow={workflow:{id:'one',status:'stopped'},stages:[{id:'choose',choice_error:'Fixture alternatives exhausted'}]};
    document.activeElement=trigger;app.$refs.choiceRetryReason={focus(){focus='retry reason';}};
    app.openChoiceRetry({id:'alpha',title:'Alpha',generated_by_stage_id:'choose',option_state:'unavailable'});
    assert.equal(app.showChoiceRetryModal,true);assert.equal(focus,'retry reason');
    const retryTarget=inside(retry);app.choiceRetrySaving=true;
    retryTarget.addEventListener('keydown',event=>{retryEscape.call(app,event);event.stopPropagation();});
    retryTarget.addEventListener('keydown',event=>workflowEscape.call(app,event));
    retryTarget.dispatchEvent(key());
    assert.equal(app.showChoiceRetryModal,true);assert.equal(app.workflowInspectorOpen,true);assert.equal(focus,'retry reason','Saving keeps focus in the live retry dialog');
    app.choiceRetrySaving=false;retryTarget.dispatchEvent(key());
    assert.equal(app.showChoiceRetryModal,false);assert.equal(app.workflowInspectorOpen,true);assert.equal(focus,'retry trigger');
    const menuTarget=inside(null);app.workflowMenuOpen=true;
    menuTarget.addEventListener('keydown',event=>workflowEscape.call(app,event));menuTarget.dispatchEvent(key());
    assert.equal(app.workflowMenuOpen,false);assert.equal(app.workflowInspectorOpen,true);assert.equal(focus,'menu');
    const inspectorTarget=inside(inspector);app._workflowInspectorReturnFocus=trigger;
    inspectorTarget.addEventListener('keydown',event=>workflowEscape.call(app,event));inspectorTarget.dispatchEvent(key());
    assert.equal(app.workflowInspectorOpen,false,'Escape originating in the inspector still closes it');
    """)


def test_workflow_drawer_uses_css_media_query_at_fractional_widths():
    run_ui("""
    const app=waylineApp();let matches=false,queries=[];
    global.window={innerWidth:959.5,matchMedia(query){queries.push(query);return {matches};}};
    app.syncWorkflowInspectorViewport();assert.equal(app.workflowInspectorOverlay,false,'959.5px is outside CSS max-width959px despite being below960');
    matches=true;window.innerWidth=959;app.syncWorkflowInspectorViewport();assert.equal(app.workflowInspectorOverlay,true);
    matches=false;window.innerWidth=960;app.syncWorkflowInspectorViewport();assert.equal(app.workflowInspectorOverlay,false);
    assert.deepEqual(queries,['(max-width: 959px)','(max-width: 959px)','(max-width: 959px)']);
    """)
