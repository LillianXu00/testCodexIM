"""Approval flow helper for isolated integration services, never production auto-approval."""
async def enroll_test_identity(http, central_url, agent_id):
    response=await http.post(central_url+'/api/v3/admission/apply',json={
        'agent_id':agent_id,'name':agent_id,'purpose':'isolated integration test'})
    response.raise_for_status();application=response.json()
    response=await http.post(central_url+'/api/v3/admission/admin/decision',
        headers={'Authorization':'Bearer test-admin'},json={'application_id':application['application_id'],'action':'approve','reason':'isolated test'})
    response.raise_for_status();approved=response.json()
    response=await http.post(central_url+'/api/v3/admission/redeem',json={
        'application_id':application['application_id'],'registration_code':approved['registration_code']})
    response.raise_for_status();return response.json()
