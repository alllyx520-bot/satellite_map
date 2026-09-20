from pathlib import Path
from django.http import FileResponse, HttpResponse
from django.urls import path
from PIL import Image
from ..models import SpatialAttachment, SpatialObservation
from . import assets
from .common import APIError, body, endpoint, owned_conversation, owner

def _att(r,i): return SpatialAttachment.objects.filter(pk=i,owner_session_key=owner(r)).first()
@endpoint('POST')
def uploads(r):
 d=body(r); c=owned_conversation(r,d['conversation_id']) if d.get('conversation_id') else None
 if d.get('conversation_id') and not c: raise APIError('会话不存在','not_found',404)
 try:u=assets.create_upload(owner(r),d.get('name'),d.get('size_bytes'),c)
 except (ValueError,TypeError) as e: raise APIError(str(e),'invalid_upload')
 return {'id':str(u.id),'chunk_size':u.chunk_size,'received_chunks':[]}
@endpoint('PUT')
def upload_chunk(r,upload_id,index):
 try:u=assets.upload_chunk(upload_id,owner(r),int(index),r)
 except LookupError: raise APIError('上传不存在','not_found',404)
 except (ValueError,TypeError) as e: raise APIError(str(e),'invalid_chunk')
 return {'id':str(u.id),'received_chunks':assets.received_indexes(u)}
@endpoint('POST')
def upload_complete(r,upload_id):
 try:a=assets.complete_upload(upload_id,owner(r))
 except LookupError: raise APIError('上传不存在','not_found',404)
 except ValueError as e: raise APIError(str(e),'invalid_upload')
 return {'attachment':assets.attachment_payload(a)}
@endpoint('GET')
def upload_detail(r,upload_id):
 try:u=assets._owned_upload(upload_id,owner(r))
 except LookupError: raise APIError('上传不存在','not_found',404)
 return {'id':str(u.id),'status':u.status,'size_bytes':u.size_bytes,'chunk_size':u.chunk_size,'received_chunks':assets.received_indexes(u),'attachment':assets.attachment_payload(u.attachment)}
@endpoint('GET')
def attachment_detail(r,attachment_id):
 a=_att(r,attachment_id)
 if not a: raise APIError('附件不存在','not_found',404)
 return {'attachment':assets.attachment_payload(a)}
def attachment_preview(r,attachment_id):
 a=_att(r,attachment_id)
 if not a or not a.preview_path or not Path(a.preview_path).is_file(): raise APIError('预览不存在','not_found',404)
 return FileResponse(open(a.preview_path,'rb'),content_type='image/jpeg')
def observation_preview(r,observation_id):
 o=SpatialObservation.objects.select_related('attachment').filter(pk=observation_id,attachment__owner_session_key=owner(r)).first()
 if not o or not o.preview_path or not Path(o.preview_path).is_file(): raise APIError('预览不存在','not_found',404)
 return FileResponse(open(o.preview_path,'rb'),content_type='image/jpeg')
def attachment_tile(r,attachment_id,z,x,y):
 a=_att(r,attachment_id)
 if not a or a.status!='ready': raise APIError('附件不存在或未就绪','not_found',404)
 try:
  return HttpResponse(assets.render_tile(a,int(z),int(x),int(y)),content_type='image/png')
 except (ValueError,OSError): raise APIError('瓦片坐标无效','invalid_tile',404)
@endpoint('POST')
def attachment_window(r,attachment_id):
 a=_att(r,attachment_id)
 if not a: raise APIError('附件不存在','not_found',404)
 try:
  data=body(r)
  if set(data)-{'x','y','width','height','max_size'}: raise ValueError('窗口请求包含不允许的字段')
  return {'observation':assets.observation_payload(assets.read_window(a,**data)['observation'])}
 except (ValueError,TypeError) as e: raise APIError(str(e),'invalid_window')
@endpoint('POST')
def map_attachment(r):
 d=body(r); b=d.get('bbox')
 if isinstance(b,list) and len(b)==4:b=dict(zip(('min_lng','min_lat','max_lng','max_lat'),b))
 if not isinstance(b,dict): raise APIError('bbox 必须为数组或对象','invalid_attachment')
 try:b={k:float(b[k]) for k in ('min_lng','min_lat','max_lng','max_lat')}
 except (KeyError,TypeError,ValueError): raise APIError('bbox 无效','invalid_attachment')
 if not (-180<=b['min_lng']<b['max_lng']<=180 and -90<=b['min_lat']<b['max_lat']<=90): raise APIError('bbox 无效','invalid_attachment')
 c=owned_conversation(r,d['conversation_id']) if d.get('conversation_id') else None
 if d.get('conversation_id') and not c: raise APIError('会话不存在','not_found',404)
 source=d.get('source') or 'esri'
 if source not in {'mapbox','tianditu','esri'}: raise APIError('不支持的影像源','invalid_attachment')
 a=SpatialAttachment.objects.create(owner_session_key=owner(r),conversation=c,name=f'map-{source}.tif',kind='image',status='pending',coordinate_space='geographic',bbox=b,geometry=d.get('geometry'),metadata={'source':source,'bbox':b,'map_request':d})
 return {'attachment':assets.attachment_payload(a)}

urlpatterns=[path('uploads',uploads),path('uploads/<uuid:upload_id>',upload_detail),path('uploads/<uuid:upload_id>/chunks/<int:index>',upload_chunk),path('uploads/<uuid:upload_id>/complete',upload_complete),path('attachments',map_attachment),path('attachments/<uuid:attachment_id>',attachment_detail),path('attachments/<uuid:attachment_id>/preview',endpoint('GET')(attachment_preview)),path('attachments/<uuid:attachment_id>/tiles/<int:z>/<int:x>/<int:y>.png',endpoint('GET')(attachment_tile)),path('attachments/<uuid:attachment_id>/windows',attachment_window),path('observations/<uuid:observation_id>/preview',endpoint('GET')(observation_preview))]
