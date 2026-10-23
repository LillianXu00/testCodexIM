import argparse
import asyncio
import json
from pathlib import Path
from pydantic import validate_call
from .sdk import WorkBuddyClient, TOOLS


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['serve','call','tools'])
    parser.add_argument('tool',nargs='?')
    parser.add_argument('--profile',type=Path)
    parser.add_argument('--args',default='{}')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=13902)
    args=parser.parse_args()
    if args.command=='serve':
        import uvicorn
        uvicorn.run('planetbaltan_workbuddy.app:create_app',factory=True,host=args.host,port=args.port)
    elif args.command=='tools':print(json.dumps(TOOLS))
    else:
        if not args.profile or args.tool not in TOOLS:parser.error('profile and valid tool required')
        async def call():
            p=json.loads(args.profile.read_text())
            client=WorkBuddyClient(p['url'],p['credential'],communication_url=p['communication_url'])
            try:return await validate_call(getattr(client,args.tool))(**json.loads(args.args))
            finally:await client.close()
        print(json.dumps(asyncio.run(call()),ensure_ascii=False))

if __name__=='__main__':main()
