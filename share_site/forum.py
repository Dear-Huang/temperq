"""Persistent research discussions, linked optionally to a published model."""

from fastapi import APIRouter, Form, HTTPException, Request

CATEGORIES = {"models", "recipes", "performance", "questions"}


def register_forum(app, con, user, csrf, check_csrf, now, index):
    con.executescript("""
    create table if not exists topics(
      id integer primary key, owner text not null, title text not null,
      category text not null, body text not null, recipe text not null default '',
      share_id integer references shares(id), created_at text not null, updated_at text not null);
    create table if not exists replies(
      id integer primary key, topic_id integer not null references topics(id),
      owner text not null, body text not null, created_at text not null);
    create index if not exists replies_topic on replies(topic_id, id);
    """)
    router = APIRouter()

    def require_user(req, token):
        owner = user(req)
        if not owner:
            raise HTTPException(401, "请先登录后参与讨论")
        check_csrf(req, token)
        return owner

    def topic_row(tid):
        row = con.execute("select * from topics where id=?", (tid,)).fetchone()
        if not row:
            raise HTTPException(404, "讨论不存在")
        return dict(row)

    @router.get("/community")
    @router.get("/community/new")
    def forum_page(req: Request):
        return index(req)

    @router.get("/community/t/{tid}")
    def topic_page(req: Request, tid: int):
        topic_row(tid)
        return index(req)

    @router.get("/api/forum")
    def topics(req: Request):
        rows = con.execute("""
          select t.id,t.owner,t.title,t.category,substr(t.body,1,240) as excerpt,
                 t.created_at,t.updated_at,t.share_id,s.repo,
                 (select count(*) from replies where topic_id=t.id) as reply_count
          from topics t left join shares s on s.id=t.share_id
          order by t.updated_at desc,t.id desc limit 200
        """).fetchall()
        return {"topics": [dict(r) for r in rows]}

    @router.get("/api/forum/{tid}")
    def topic(tid: int):
        result = topic_row(tid)
        result["replies"] = [dict(r) for r in con.execute(
            "select * from replies where topic_id=? order by id", (tid,))]
        linked = con.execute("select id,repo,sha,research from shares where id=?",
                             (result["share_id"],)).fetchone()
        result["model"] = dict(linked) if linked else None
        return result

    @router.post("/api/forum")
    def publish(req: Request, token: str = Form(""), title: str = Form(..., max_length=120),
                category: str = Form(...), body: str = Form(..., max_length=30000),
                recipe: str = Form("", max_length=8000), share_id: int | None = Form(None)):
        owner = require_user(req, token)
        if category not in CATEGORIES or len(title.strip()) < 4 or len(body.strip()) < 10:
            raise HTTPException(422, "请选择板块，标题至少 4 字，正文至少 10 字")
        if share_id is not None:
            model = con.execute("select owner from shares where id=?", (share_id,)).fetchone()
            if not model or model["owner"] != owner:
                raise HTTPException(403, "只能关联自己发布的模型")
        date = now()
        with con:
            cur = con.execute("insert into topics(owner,title,category,body,recipe,share_id,created_at,updated_at) "
                              "values (?,?,?,?,?,?,?,?)", (owner,title.strip(),category,body.strip(),
                              recipe,share_id,date,date))
        return {"id": cur.lastrowid, "url": f"/community/t/{cur.lastrowid}"}

    @router.post("/api/forum/{tid}/replies")
    def reply(req: Request, tid: int, token: str = Form(""),
              body: str = Form(..., max_length=5000)):
        owner = require_user(req, token)
        topic_row(tid)
        if not body.strip():
            raise HTTPException(422, "请输入回复内容")
        date = now()
        with con:
            con.execute("insert into replies(topic_id,owner,body,created_at) values (?,?,?,?)",
                        (tid,owner,body.strip(),date))
            con.execute("update topics set updated_at=? where id=?", (date,tid))
        return {"ok": True}

    app.include_router(router)
