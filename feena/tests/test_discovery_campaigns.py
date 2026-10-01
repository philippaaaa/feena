import asyncio
import json

import pytest
import yaml

from feena.campaigns import Campaigns
from feena.simulation_config import BrowserScenario


def manager(tmp_path):
    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump({'target': {'compose': 'compose.yaml', 'service': 'app', 'port': 5056}, 'discovery': [{
        'name': 'discover-checkout', 'goal': 'Complete checkout', 'start_path': '/',
        'assertions': [{'kind': 'visible', 'target': '#confirmation'}]}]}))
    return Campaigns(config, ['http://127.0.0.1:5056'], tmp_path / 'store')


def propose(runs, job):
    goal = json.loads(job['snapshot'])
    draft = BrowserScenario(name=goal['name'], goal=goal['goal'],
                            steps=[{'action': 'goto', 'target': '/'}],
                            assertions=goal['assertions']).model_dump_json()
    with runs.db:
        runs.db.execute('UPDATE jobs SET draft=? WHERE id=?', (draft, job['id']))
    return 'proposed', 'Review required'


def test_missing_provider_key_is_clear(tmp_path, monkeypatch):
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    async def check():
        runs = manager(tmp_path)
        campaign = await runs.start_discovery('discover-checkout')
        await asyncio.gather(*runs.tasks.values())
        result = runs.get_discovery(campaign['campaign_id'])
        assert result['status'] == 'inconclusive'
        assert 'ANTHROPIC_API_KEY' in result['jobs'][0]['reason']
        assert not result['requires_approval']
        await runs.close()
    asyncio.run(check())


def test_restart_review_and_idempotent_promotion(tmp_path, monkeypatch):
    async def check():
        runs = manager(tmp_path)
        async def discover(job, target):
            return propose(runs, job)
        monkeypatch.setattr(runs, '_execute', discover)
        campaign = await runs.start_discovery('discover-checkout')
        await asyncio.gather(*runs.tasks.values())
        assert runs.get_discovery(campaign['campaign_id'])['requires_approval']
        await runs.close()
        runs = manager(tmp_path)
        assert runs.get_discovery(campaign['campaign_id'])['status'] == 'proposed'
        executed = []
        async def execute(job, target):
            executed.append(job['kind'])
            return 'passed', None
        monkeypatch.setattr(runs, '_execute', execute)
        first, second = await asyncio.gather(runs.approve_discovery(campaign['campaign_id']),
                                           runs.approve_discovery(campaign['campaign_id']))
        assert first['campaign_id'] == second['campaign_id']
        await asyncio.gather(*runs.tasks.values())
        assert executed == ['simulation']
        assert runs.list_scenarios()[0]['name'] == 'discover-checkout'
        assert not runs.get_discovery(campaign['campaign_id'])['requires_approval']
        again = await runs.start(['discover-checkout'])
        await asyncio.gather(*runs.tasks.values())
        assert runs.get(again['campaign_id'])['status'] == 'completed'
        await runs.close()
    asyncio.run(check())


@pytest.mark.parametrize('field,value', [('assertions', [{'kind': 'visible', 'target': '#anything'}]),
                                        ('goal', 'Different goal'), ('steps', [{'action': 'goto', 'target': '/elsewhere'}]),
                                        ('profiles', [{'name': 'broken', 'effect': 'abort'}])])
def test_modified_draft_cannot_be_approved(tmp_path, monkeypatch, field, value):
    async def check():
        runs = manager(tmp_path)
        async def discover(job, target):
            return propose(runs, job)
        monkeypatch.setattr(runs, '_execute', discover)
        campaign = await runs.start_discovery('discover-checkout')
        await asyncio.gather(*runs.tasks.values())
        job = runs._discovery_job(campaign['campaign_id'])
        draft = json.loads(job['draft'])
        draft[field] = value
        with runs.db:
            runs.db.execute('UPDATE jobs SET draft=? WHERE id=?', (json.dumps(draft), job['id']))
        with pytest.raises(ValueError):
            await runs.approve_discovery(campaign['campaign_id'])
        assert runs.db.execute('SELECT count(*) FROM approved_discoveries').fetchone()[0] == 0
        await runs.close()
    asyncio.run(check())


def test_discovery_and_simulation_share_queue(tmp_path, monkeypatch):
    async def check():
        runs = manager(tmp_path)
        runs.scenarios = [BrowserScenario(name='existing', goal='Existing', steps=[{'action': 'goto', 'target': '/'}],
                                          assertions=[{'kind': 'visible', 'target': '#ok'}])]
        order = []
        async def execute(job, target):
            order.append(job['kind'])
            await asyncio.sleep(0)
            return propose(runs, job) if job['kind'] == 'discovery' else ('passed', None)
        monkeypatch.setattr(runs, '_execute', execute)
        discovery = await runs.start_discovery('discover-checkout')
        simulation = await runs.start(['existing'])
        await asyncio.gather(*runs.tasks.values())
        assert order == ['discovery', 'simulation']
        assert runs.get(discovery['campaign_id'])['status'] == 'proposed'
        assert runs.get(simulation['campaign_id'])['status'] == 'completed'
        await runs.close()
    asyncio.run(check())


def test_worker_contract_secret_isolation_and_failed_validation(tmp_path, monkeypatch):
    from pathlib import Path
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'provider-test')
    monkeypatch.setenv('FEENA_MCP_TOKEN', 'private-token')
    seen = []
    class Process:
        pid = 99999999
        returncode = 0
        async def wait(self):
            return 0
    async def spawn(*args, **kwargs):
        folder = Path(args[-2])
        assert 'FEENA_MCP_TOKEN' not in kwargs['env']
        if args[2] == 'feena.discovery_worker':
            assert kwargs['env']['ANTHROPIC_API_KEY'] == 'provider-test'
            goal = json.loads((folder / 'discovery.json').read_text())
            scenario = BrowserScenario(name=goal['name'], goal=goal['goal'],
                                       steps=[{'action': 'goto', 'target': '/'}],
                                       assertions=goal['assertions'])
            (folder / 'discovery-results.json').write_text(json.dumps({'status': 'proposed', 'scenario': scenario.model_dump()}))
        else:
            assert 'ANTHROPIC_API_KEY' not in kwargs['env']
            (folder / 'results.json').write_text('[{"status":"failed"}]')
        seen.append(args[2])
        return Process()
    monkeypatch.setattr('feena.campaigns.asyncio.create_subprocess_exec', spawn)
    monkeypatch.setattr('feena.campaigns.os.killpg', lambda *args: None)
    async def check():
        runs = manager(tmp_path)
        discovery = await runs.start_discovery('discover-checkout')
        await asyncio.gather(*runs.tasks.values())
        assert runs.get_discovery(discovery['campaign_id'])['scenario']['name'] == 'discover-checkout'
        replay = await runs.approve_discovery(discovery['campaign_id'])
        assert runs.list_scenarios() == []
        await asyncio.gather(*runs.tasks.values())
        assert runs.get(replay['campaign_id'])['status'] == 'failed'
        assert runs.list_scenarios() == []
        assert runs.get_discovery(discovery['campaign_id'])['validation_status'] == 'failed'
        assert seen == ['feena.discovery_worker', 'feena.mcp_worker']
        await runs.close()
    asyncio.run(check())
