import json
from pathlib import Path

class GridEndpoint:
    def __init__(self,path="fixtures/points.json",cap=3):
        self.points=json.loads(Path(path).read_text());self.cap=cap;self.calls=[]
    def query(self,west,south,east,north):
        if not (-180<=west<=east<=180 and -90<=south<=north<=90):
            raise ValueError("接口只接受不跨经线的有序矩形")
        self.calls.append([west,south,east,north])
        selected=[p for p in self.points if west<=p["lon"]<=east and south<=p["lat"]<=north]
        return {"points":selected[:self.cap],"count":min(len(selected),self.cap),"truncated":len(selected)>self.cap}
