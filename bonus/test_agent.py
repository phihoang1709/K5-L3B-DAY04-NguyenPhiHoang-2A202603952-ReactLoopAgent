"""Kiểm tra agent với tools bonus mà không gọi LLM API."""
import json
import sys
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent import run_agent
from bonus.tools import BonusTools


def call(call_id,name,args):
    raw=json.dumps(args)
    c={'id':call_id,'name':name,'arguments':raw}
    m={'role':'assistant','content':None,'tool_calls':[{'id':call_id,'type':'function','function':{'name':name,'arguments':raw}}]}
    return {'message':m,'calls':[c],'finish':'tool_calls','usage':{}}

def final(answer='Đã kiểm tra'):
    return {'message':{'role':'assistant','content':answer},'calls':[],'finish':'stop','usage':{}}


class ScriptedProvider:
    def __init__(self,responses): self.responses=iter(responses); self.histories=[]; self.schemas=[]
    def complete(self,messages,schemas):
        self.histories.append(list(messages));self.schemas.append(schemas)
        return next(self.responses)


class BonusAgentTests(unittest.TestCase):
    def test_provider_names_are_not_mistaken_for_incident_ids(self):
        t=BonusTools();p=ScriptedProvider([final('Đã tổng hợp năm nhà cung cấp.')])

        result=run_agent(
            'Tổng hợp GitHub, Cloudflare, Vercel, npm và DigitalOcean.',p,t
        )

        self.assertEqual(result['status'],'completed')
        self.assertFalse(any(
            x['event']=='validation_unfollowed_incident' for x in result['trace']
        ))

    def test_bonus_schemas_and_system_prompt(self):
        t=BonusTools();p=ScriptedProvider([final()])
        self.assertEqual(run_agent('Kiểm tra',p,t)['status'],'completed')
        names={x['function']['name'] for x in p.schemas[0]}
        self.assertTrue({'get_github_status','get_cloudflare_status','get_vercel_status','get_npm_status','get_digitalocean_status'}.issubset(names))
        self.assertIn('DigitalOcean',p.histories[0][0]['content'])
    def test_429_observation_returns_to_model(self):
        t=BonusTools(scenario='rate_limit_once')
        p=ScriptedProvider([call('a','get_vercel_status',{}),call('b','get_vercel_status',{}),final()])
        result=run_agent('Kiểm tra',p,t,max_steps=5)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(json.loads(p.histories[1][-1]['content'])['error'],'RATE_LIMITED')
        self.assertEqual(json.loads(p.histories[2][-1]['content'])['provider'],'vercel')
    def test_invalid_arguments_observation_returns_to_model(self):
        t=BonusTools()
        bad={'provider':'npm','incident_id':'cc0t2r4403sy','offset':'zero'}
        good={'provider':'npm','incident_id':'cc0t2r4403sy','offset':0}
        details=t.execute('get_incident_details',good)
        p=ScriptedProvider([call('a','get_incident_details',bad),call('b','get_incident_details',good),final('Nguồn: '+details['url'])])
        result=run_agent('Kiểm tra',p,t,max_steps=5)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(json.loads(p.histories[1][-1]['content'])['error'],'INVALID_ARGUMENTS')
        self.assertTrue(json.loads(p.histories[2][-1]['content'])['ok'])

    def test_retriable_429_blocks_early_final_until_retry_succeeds(self):
        t=BonusTools(scenario='rate_limit_once')
        p=ScriptedProvider([
            call('a','get_vercel_status',{}),
            final('Chưa kiểm tra được.'),
            call('b','get_vercel_status',{}),
            final('Vercel operational. Nguồn: https://www.vercel-status.com'),
        ])

        result=run_agent('Kiểm tra Vercel, retry có giới hạn',p,t,max_steps=6)

        self.assertEqual(result['status'],'completed')
        self.assertEqual(sum(x['event']=='tool' for x in result['trace']),2)
        self.assertTrue(any(x['event']=='validation_retry_required' for x in result['trace']))

    def test_persistent_429_stops_forcing_after_three_attempts(self):
        t=BonusTools(scenario='rate_limit_always')
        p=ScriptedProvider([
            call('a','get_vercel_status',{}), final('Chưa xác minh.'),
            call('b','get_vercel_status',{}), final('Vẫn chưa xác minh.'),
            call('c','get_vercel_status',{}), final('Không thể xác minh sau 3 lần.'),
        ])

        result=run_agent('Kiểm tra Vercel, tối đa 3 lần',p,t,max_steps=6)

        self.assertEqual(result['status'],'completed')
        self.assertEqual(sum(x['event']=='tool' for x in result['trace']),3)
        self.assertEqual(sum(
            x['event']=='validation_retry_required' for x in result['trace']
        ),2)

    def test_explicit_incident_id_must_be_read_before_final(self):
        t=BonusTools()
        details=t.execute('get_incident_details',{
            'provider':'vercel','incident_id':'w661l7k3kf4p'
        })
        p=ScriptedProvider([
            final('Chưa đọc incident.'),
            call('a','get_incident_details',{
                'provider':'vercel','incident_id':'w661l7k3kf4p'
            }),
            final('Đã đọc incident. Nguồn: '+details['url']),
        ])

        result=run_agent('So sánh Vercel incident w661l7k3kf4p',p,t,max_steps=5)

        self.assertEqual(result['status'],'completed')
        self.assertTrue(any(
            x['event']=='validation_unfollowed_incident' for x in result['trace']
        ))

    def test_latest_incident_from_status_must_be_read(self):
        t=BonusTools()
        status=t.execute('get_vercel_status',{'component':'Builds'})
        incident_id=status['incidents'][0]['id']
        details=t.execute('get_incident_details',{
            'provider':'vercel','incident_id':incident_id
        })
        p=ScriptedProvider([
            call('a','get_vercel_status',{'component':'Builds'}),
            final('Incident mới nhất đã resolved.'),
            call('b','get_incident_details',{
                'provider':'vercel','incident_id':incident_id
            }),
            final('Incident mới nhất đã resolved. Nguồn: '+details['url']),
        ])

        result=run_agent('Kiểm tra incident mới nhất của Vercel Builds',p,t,max_steps=6)

        self.assertEqual(result['status'],'completed')
        detail_calls=[x for x in result['trace'] if x['event']=='tool'
                      and x['name']=='get_incident_details']
        self.assertEqual(detail_calls[0]['arguments']['incident_id'],incident_id)

    def test_timeline_question_follows_all_next_offsets(self):
        t=BonusTools()
        incident_id='cc0t2r4403sy'
        details=t.execute('get_incident_details',{
            'provider':'npm','incident_id':incident_id,'offset':0,'limit':1
        })
        p=ScriptedProvider([
            call('a','get_incident_details',{
                'provider':'npm','incident_id':incident_id,'offset':0,'limit':1
            }),
            final('Đã đọc timeline. Nguồn: '+details['url']),
            call('b','get_incident_details',{
                'provider':'npm','incident_id':incident_id,'offset':1,'limit':1
            }),
            final('Đã đọc timeline. Nguồn: '+details['url']),
            call('c','get_incident_details',{
                'provider':'npm','incident_id':incident_id,'offset':2,'limit':1
            }),
            final('Đã đọc toàn bộ timeline. Nguồn: '+details['url']),
        ])

        result=run_agent('Tra timeline npm incident cc0t2r4403sy',p,t,max_steps=8)

        self.assertEqual(result['status'],'completed')
        offsets=[x['arguments'].get('offset',0) for x in result['trace']
                 if x['event']=='tool' and x['name']=='get_incident_details'
                 and x['arguments']['provider']=='npm']
        self.assertEqual(offsets,[0,1,2])
        self.assertEqual(sum(
            x['event']=='validation_pagination_required' for x in result['trace']
        ),2)

if __name__=='__main__': unittest.main()
