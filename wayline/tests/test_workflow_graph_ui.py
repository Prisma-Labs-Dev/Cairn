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
    assert.match(condition.reason,/Condition is false/);
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


def test_workflow_resize_refits_untouched_view_but_preserves_manual_camera_and_disposes():
    run_ui("""
    const app=waylineApp();app.view='workflow';app.workflow={workflow:{id:'one',status:'active'},stages:[],collections:[]};
    const timers=[];const observers=[];const graphs=[];const listeners={};let size={width:880,height:480};
    global.document={getElementById:()=>({getBoundingClientRect:()=>size,addEventListener:(name,fn)=>listeners[name]=fn,removeEventListener:(name)=>delete listeners[name]})};
    global.setTimeout=fn=>{timers.push(fn);return timers.length;};global.clearTimeout=()=>{};
    global.ResizeObserver=class {constructor(fn){this.update=fn;observers.push(this);}observe(){}disconnect(){this.disconnected=true;}};
    global.cytoscape=options=>{
      const graph={fits:0,resizes:0,on(){},elements:()=>({removeClass(){}}),getElementById:()=>({addClass(){}}),resize(){this.resizes++;},fit(){this.fits++;},destroy(){this.destroyed=true;}};
      graphs.push(graph);assert.equal(options.layout.animate,false);return graph;
    };
    app.cy={destroy(){throw Error('Project graph is a separate instance');}};
    app.initWorkflowGraph();observers[0].update();timers[0]();assert.equal(graphs[0].fits,1);
    observers[0].update();assert.equal(timers.length,1,'Repeated same-size notifications do not change camera');
    size={width:390,height:220};observers[0].update();timers[1]();assert.equal(graphs[0].fits,2,'Untouched desktop view refits on phone');
    listeners.wheel();size={width:880,height:480};observers[0].update();timers[2]();assert.equal(graphs[0].fits,2,'Manual camera is retained after resize');
    size={width:900,height:500};observers[0].update();app.disposeWorkflowGraph();
    assert.equal(graphs[0].destroyed,true);assert.equal(observers[0].disconnected,true);
    assert.deepEqual(Object.keys(listeners),[]);
    app.initWorkflowGraph();timers[3]();assert.equal(graphs[1].resizes,0,'Old pending callback cannot resize replacement workflow');
    app.disposeWorkflowGraph();
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
    assert.equal(graph.edges.filter(edge=>edge.data.id==='choice-proposal:choose').length,0);
    assert.equal(graph.edges.find(edge=>edge.data.id==='dependency:serve:0').data.source,'choice:choose');
    assert.ok(app.workflow.stages.find(stage=>stage.id==='choose').choice_error);
    app.workflowSelectedRecord={type:'edge',id:'choice-selected:choose:alpha'};
    app.ensureWorkflowSelection(graph);
    assert.deepEqual(app.workflowSelectedRecord,{type:'overview',id:'overview'});
    """)


def test_pause_response_updates_graph_before_poll_and_invalidates_older_read():
    run_ui("""
    const app=waylineApp();app.view='workflow';app.selectedWorkflowId='one';app.workflowCy={};
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
