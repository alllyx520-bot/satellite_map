import hashlib, math, os, shutil, uuid, secrets, threading, logging, re, struct
from pathlib import Path
from django.conf import settings
from django.db import transaction, close_old_connections
from django.utils import timezone
from PIL import Image, ImageOps
from ..models import AttachmentUpload, SpatialAttachment, SpatialObservation
from ..run_journal import retry_sqlite_write
from .runtime import checkpoint

CHUNK_SIZE=1024*1024; MAX_UPLOAD_BYTES=1024*1024*1024; TILE_SIZE=256
ALLOWED={'.png','.jpg','.jpeg','.tif','.tiff'}
log = logging.getLogger(__name__)
def _root():
    p=(Path(settings.MEDIA_ROOT).resolve()/'v3-assets'); p.mkdir(parents=True,exist_ok=True); return p
def _inside(p, root=None):
    # Windows resolve() may return a \\?\-prefixed extended path when the tail
    # does not exist yet; normalize both sides before the containment check.
    def norm(x):
        s=os.path.normcase(str(Path(x).resolve()))
        if s.startswith('\\\\?\\'): s='\\\\'+s[8:] if s[4:8].lower()=='unc\\' else s[4:]
        return s
    p,r=norm(p),norm(root or _root())
    if p!=r and not p.startswith(r+os.sep): raise ValueError('附件路径无效')
    return Path(p)
def _suffix(n):
    s=Path(str(n or 'image')).suffix.lower()
    if s not in ALLOWED: raise ValueError('仅支持 PNG、JPEG 或 GeoTIFF 文件')
    return s
def _safe_name(n):
    n=Path(str(n or 'image')).name
    if not n or len(n)>240: raise ValueError('附件名称无效')
    _suffix(n); return n
def adopt_file(source,name):
    """Copy a file produced outside the asset store (e.g. python_analysis output) into it."""
    t=_inside(_root()/'files'/f'{uuid.uuid4()}-{_safe_name(name)}')
    t.parent.mkdir(parents=True,exist_ok=True)
    try: shutil.copy2(source,t)
    except Exception:
        t.unlink(missing_ok=True)
        raise
    return t
def _owned_upload(i,o):
    x=AttachmentUpload.objects.select_related('attachment').filter(pk=i,attachment__owner_session_key=o).first()
    if not x: raise LookupError('上传不存在')
    return x
def received_indexes(u): return sorted(int(i) for i in (u.received_chunks or {}))
def create_upload(owner,name,size_bytes,conversation=None):
    name=_safe_name(name)
    if isinstance(size_bytes, bool) or not isinstance(size_bytes,int) or not 0<size_bytes<=MAX_UPLOAD_BYTES: raise ValueError('文件大小必须在 1B 到 1GB 之间')
    a=SpatialAttachment.objects.create(owner_session_key=owner,conversation=conversation,name=name,kind='geotiff' if _suffix(name) in {'.tif','.tiff'} else 'image',status='uploading',size_bytes=size_bytes)
    return AttachmentUpload.objects.create(attachment=a,size_bytes=size_bytes,chunk_size=CHUNK_SIZE)
def upload_chunk(upload_id,owner,index,stream):
    with transaction.atomic():
        u=AttachmentUpload.objects.select_for_update().select_related('attachment').filter(pk=upload_id,attachment__owner_session_key=owner).first()
        if not u: raise LookupError('上传不存在')
        if u.status!='uploading': raise ValueError('上传已结束')
        count=math.ceil(u.size_bytes/u.chunk_size)
        if isinstance(index, bool) or not isinstance(index,int) or index<0 or index>=count: raise ValueError('分块序号无效')
        length=min(u.chunk_size,u.size_bytes-index*u.chunk_size); folder=_root()/'uploads'/str(u.id); folder.mkdir(parents=True,exist_ok=True)
        target=_inside(folder/str(index)); temp=_inside(folder/(str(index)+'.part')); h=hashlib.sha256(); total=0
        with open(temp,'wb') as out:
            for data in iter(lambda: stream.read(1024*1024),b''):
                total+=len(data)
                if total>length: break
                h.update(data); out.write(data)
        if total>length: temp.unlink(missing_ok=True); raise ValueError('分块大小超出限制')
        if total!=length: temp.unlink(missing_ok=True); raise ValueError('分块大小不匹配')
        old=(u.received_chunks or {}).get(str(index)); digest=h.hexdigest()
        if old:
            temp.unlink(missing_ok=True)
            if old.get('sha256')!=digest or old.get('size')!=total: raise ValueError('重复分块内容冲突')
            return u
        os.replace(temp,target); rec=dict(u.received_chunks or {}); rec[str(index)]={'size':total,'sha256':digest}; u.received_chunks=rec; u.save(update_fields=['received_chunks','updated_at']); return u
def complete_upload(upload_id,owner):
    with transaction.atomic():
        u=AttachmentUpload.objects.select_for_update().select_related('attachment').filter(pk=upload_id,attachment__owner_session_key=owner).first()
        if not u: raise LookupError('上传不存在')
        if u.status=='completed': return u.attachment
        count=math.ceil(u.size_bytes/u.chunk_size); rec=u.received_chunks or {}
        if set(rec)!={str(i) for i in range(count)}: raise ValueError('上传分块不完整')
        folder=_root()/'uploads'/str(u.id)
        for i in range(count):
            p=_inside(folder/str(i)); e=rec[str(i)]
            if not p.is_file() or p.stat().st_size!=e['size'] or hashlib.sha256(p.read_bytes()).hexdigest()!=e['sha256']: raise ValueError('上传分块校验失败')
        a=u.attachment; final=_inside(_root()/'files'/f'{a.id}{_suffix(a.name)}'); final.parent.mkdir(parents=True,exist_ok=True); temp=_inside(final.with_suffix(final.suffix+'.part')); h=hashlib.sha256()
        with open(temp,'wb') as out:
            for i in range(count):
                with open(_inside(folder/str(i)),'rb') as part:
                    for b in iter(lambda: part.read(1024*1024),b''): h.update(b); out.write(b)
        os.replace(temp,final); a.file_path=str(final); a.sha256=h.hexdigest(); a.status='pending'; a.save(update_fields=['file_path','sha256','status','updated_at']); u.status='completed'; u.save(update_fields=['status','updated_at']); shutil.rmtree(folder,ignore_errors=True); return a
def _rgb(data, stretch=None):
    import numpy as np
    source_dtype = data.dtype
    d=np.ma.filled(data.astype('float32'), np.nan) if np.ma.isMaskedArray(data) else np.asarray(data,dtype='float32'); d=d if d.ndim==3 else d[None,...]; b=d[:3]
    if b.shape[0]==1:b=np.repeat(b,3,axis=0)
    if b.shape[0]==2:b=np.concatenate([b,b[1:2]],axis=0)
    out=np.zeros_like(b,dtype='float32')
    for i,x in enumerate(b):
        f=np.isfinite(x)
        if f.any():
            # Eight-bit imagery is already display-ready; preserving its range
            # avoids a different contrast stretch for each tile/window.
            if source_dtype == np.uint8:
                out[i] = x
            else:
                lo,hi=stretch[i] if stretch else np.percentile(x[f],[2,98]); hi=hi if hi>lo else lo+1; out[i]=np.clip((x-lo)*255/(hi-lo),0,255)
    out[~np.isfinite(out)] = 0
    return out.astype('uint8')

def _display_indexes(a, dataset):
    mapping = (a.metadata or {}).get('band_map') or {}
    if all(name in mapping for name in ('red', 'green', 'blue')):
        return [mapping[name] for name in ('red', 'green', 'blue')]
    return list(range(1, min(3, dataset.count) + 1))
def _bbox(bounds,crs):
    if not crs:return None
    try:
        import pyproj; t=pyproj.Transformer.from_crs(crs,'EPSG:4326',always_xy=True); pts=[t.transform(x,y) for x,y in ((bounds.left,bounds.bottom),(bounds.left,bounds.top),(bounds.right,bounds.bottom),(bounds.right,bounds.top))]; xs,ys=zip(*pts); return {'min_lng':min(xs),'min_lat':min(ys),'max_lng':max(xs),'max_lat':max(ys)}
    except Exception:return None
def _raster_info(p):
    import rasterio
    with rasterio.open(p) as s:return s.width,s.height,str(s.crs or ''),list(s.transform)[:6],_bbox(s.bounds,s.crs),s.count
def pixel_to_world(transform, x, y):
    """Apply the stored six-value Affine transform, including rotation."""
    a,b,c,d,e,f = transform
    return a*x + b*y + c, d*x + e*y + f
def world_to_pixel(transform, x, y):
    a,b,c,d,e,f = transform; determinant=a*e-b*d
    if not determinant: raise ValueError('仿射变换不可逆')
    return ((e*(x-c)-b*(y-f))/determinant, (-d*(x-c)+a*(y-f))/determinant)

def _exif_orientation(path):
    """Read bounded EXIF metadata without opening/decompressing a huge image."""
    with open(path, 'rb') as stream:
        signature = stream.read(8)
        if signature == b'\x89PNG\r\n\x1a\n':
            for _ in range(4096):
                header = stream.read(8)
                if len(header) != 8: return None
                length, kind = struct.unpack('>I4s', header)
                if kind == b'eXIf' and length <= 1024 * 1024:
                    exif = Image.Exif(); exif.load(stream.read(length)); return exif.get(274)
                if kind == b'IEND': return None
                stream.seek(length + 4, 1)
        elif signature[:2] == b'\xff\xd8':
            stream.seek(2)
            for _ in range(4096):
                marker = stream.read(2)
                if len(marker) != 2 or marker[0] != 255: return None
                if marker[1] in {0xda, 0xd9}: return None
                raw_length = stream.read(2)
                if len(raw_length) != 2: return None
                length = struct.unpack('>H', raw_length)[0] - 2
                if length < 0: return None
                data = stream.read(length) if marker[1] == 0xe1 else None
                if data and data.startswith(b'Exif\x00\x00'):
                    exif = Image.Exif(); exif.load(data); return exif.get(274)
                if data is None: stream.seek(length, 1)
    return None
def _canonical(a):
    """Stream scanline strips into a tiled derivative; keep original bytes."""
    p = _inside(a.file_path)
    import rasterio
    import numpy as np
    if p.suffix.lower() in {'.tif', '.tiff'}:
        with rasterio.open(p) as source:
            has_pyramid = bool(source.overviews(1)) or max(source.width, source.height) <= 512
            tiled = source.is_tiled
        if has_pyramid and tiled:
            return p
        from rasterio.shutil import copy as copy_raster
        target = _inside(_root() / 'files' / f'{a.id}-pyramid.tif')
        copy_raster(p, target, driver='COG', BLOCKSIZE=256, COMPRESS='DEFLATE',
                    BIGTIFF='IF_SAFER', OVERVIEWS='AUTO', RESAMPLING='NEAREST')
        a.file_path = str(target)
        a.metadata = {**(a.metadata or {}), 'original_path': str(p), 'original_sha256': a.sha256,
                      'streamed_ingestion': True}
        return target
    # GDAL does not expose PNG/JPEG EXIF consistently.  Pillow reads the
    # embedded metadata directly, with GDAL tags retained as a fallback for
    # drivers that do expose it.
    try:
        value = _exif_orientation(p)
    except (OSError, SyntaxError):
        value = None
    if value is None:
        with rasterio.open(p) as header:
            value = (header.tags(ns='EXIF') or {}).get('EXIF_Orientation') or header.tags().get('EXIF_Orientation', '1')
    matched = re.match(r'[1-8]', str(value))
    orientation = int(matched.group()) if matched else 1
    target = _inside(_root() / 'files' / f'{a.id}.tif')
    target.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.Env(GDAL_CACHEMAX=64 * 1024 * 1024), rasterio.open(p) as source:
        sw, sh = source.width, source.height
        rotated = orientation in {5, 6, 7, 8}
        dw, dh = (sh, sw) if rotated else (sw, sh)
        alpha_index = next((i + 1 for i, color in enumerate(source.colorinterp)
                            if color == rasterio.enums.ColorInterp.alpha), None)
        indexes = [i for i in range(1, source.count + 1) if i != alpha_index][:3]
        palette = source.colormap(1) if source.colorinterp[0] == rasterio.enums.ColorInterp.palette else None
        profile = {'driver': 'GTiff', 'width': dw, 'height': dh, 'count': 3 if palette else len(indexes),
                   'dtype': source.dtypes[0], 'tiled': True, 'blockxsize': 256,
                   'blockysize': 256, 'compress': 'deflate', 'BIGTIFF': 'IF_SAFER'}
        if a.bbox:
            from rasterio.transform import from_bounds
            from pyproj import Transformer
            b = a.bbox
            projection = Transformer.from_crs('EPSG:4326', 'EPSG:3857', always_xy=True)
            x1, y1 = projection.transform(b['min_lng'], b['min_lat'])
            x2, y2 = projection.transform(b['max_lng'], b['max_lat'])
            profile.update(crs='EPSG:3857', transform=from_bounds(x1, y1, x2, y2, dw, dh))
        strip = max(1, min(256, (8 * 1024 * 1024) // max(1, sw * len(indexes) * np.dtype(source.dtypes[0]).itemsize)))
        with rasterio.open(target, 'w', **profile) as output:
            for y in range(0, sh, strip):
                checkpoint()
                height = min(strip, sh - y)
                data = source.read(indexes, window=((y, y + height), (0, sw)))
                if palette:
                    lookup = np.zeros((max(palette) + 1, 4), dtype='uint8')
                    for index, color in palette.items():
                        lookup[index] = color
                    rgba = lookup[data[0]]
                    data = (rgba[..., :3].astype('uint16') * rgba[..., 3:4] // 255).astype('uint8').transpose(2, 0, 1)
                elif alpha_index:
                    alpha = source.read(alpha_index, window=((y, y + height), (0, sw)))
                    maximum = np.iinfo(alpha.dtype).max
                    data = (data.astype('float32') * alpha[None] / maximum).astype(data.dtype)
                xout, yout = 0, y
                if orientation == 2:
                    data = data[:, :, ::-1]
                elif orientation == 3:
                    data, yout = data[:, ::-1, ::-1], sh - y - height
                elif orientation == 4:
                    data, yout = data[:, ::-1, :], sh - y - height
                elif orientation == 5:
                    data, xout, yout = data.transpose(0, 2, 1), y, 0
                elif orientation == 6:
                    data, xout, yout = np.rot90(data, -1, axes=(1, 2)), sh - y - height, 0
                elif orientation == 7:
                    data, xout, yout = data.transpose(0, 2, 1)[:, ::-1, ::-1], sh - y - height, 0
                elif orientation == 8:
                    data, xout, yout = np.rot90(data, 1, axes=(1, 2)), y, 0
                output.write(data, window=((yout, yout + data.shape[1]), (xout, xout + data.shape[2])))
            factors = [2 ** i for i in range(1, 16) if min(dw, dh) // 2 ** i >= 128]
            if factors:
                output.build_overviews(factors, rasterio.enums.Resampling.average)
    a.file_path = str(target)
    original_to_image = {1: [1,0,0,0,1,0], 2: [-1,0,sw,0,1,0], 3: [-1,0,sw,0,-1,sh],
        4: [1,0,0,0,-1,sh], 5: [0,1,0,1,0,0], 6: [0,-1,sh,1,0,0],
        7: [0,-1,sh,-1,0,sw], 8: [0,1,0,-1,0,sw]}.get(orientation, [1,0,0,0,1,0])
    a.metadata = {**(a.metadata or {}), 'source_format': p.suffix.lower(), 'original_path': str(p),
                  'original_sha256': a.sha256, 'exif_orientation': orientation,
                  'original_size': [sw, sh], 'original_to_image_transform': original_to_image,
                  'streamed_ingestion': True}
    return target
def _preview(a):
    import rasterio, numpy as np
    from rasterio.enums import Resampling
    with rasterio.open(a.file_path) as s:
        a.metadata = {**(a.metadata or {}), 'crs_wkt': s.crs.to_wkt() if s.crs else None}
        indexes = _display_indexes(a, s)
        r=min(1,1536/max(s.width,s.height)); sh=(max(1,round(s.height*r)),max(1,round(s.width*r))); d=s.read(indexes,out_shape=(len(indexes),*sh),resampling=Resampling.bilinear,masked=True)
    stretch = []
    for band in d:
        valid = band.compressed()
        valid = valid[np.isfinite(valid)]
        stretch.append([float(v) for v in np.percentile(valid, [2, 98])] if valid.size else [0, 1])
    while len(stretch) < 3:
        stretch.append(stretch[-1])
    a.metadata = {**(a.metadata or {}), 'display_stretch': stretch}
    t=_inside(_root()/'previews'/f'{a.id}.jpg'); t.parent.mkdir(parents=True,exist_ok=True); Image.fromarray(np.moveaxis(_rgb(d,stretch),0,-1)).save(t,'JPEG',quality=88,optimize=True); return str(t)
def _claim(i):
    token=secrets.token_hex(16)
    with transaction.atomic():
        a=SpatialAttachment.objects.select_for_update().get(pk=i)
        if a.status=='ready' or (a.status=='processing' and a.processing_lease_until and a.processing_lease_until>timezone.now()): return None
        a.status='processing'; a.processing_claim=token; a.processing_lease_until=timezone.now()+timezone.timedelta(minutes=10); a.save(update_fields=['status','processing_claim','processing_lease_until','updated_at'])
    return token
def process_attachment(i):
    token=_claim(i); a=SpatialAttachment.objects.get(pk=i)
    if token is None:return a
    stop = threading.Event()
    def renew():
        while not stop.wait(30):
            close_old_connections()
            try:
                SpatialAttachment.objects.filter(pk=i, processing_claim=token).update(
                    processing_lease_until=timezone.now()+timezone.timedelta(minutes=10))
            except Exception:
                log.warning('Unable to renew attachment lease for %s', i)
            finally:
                close_old_connections()
    heartbeat = threading.Thread(target=renew, daemon=True)
    heartbeat.start()
    try:
        # Map requests deliberately defer network work to this worker path.
        if not a.file_path and (a.metadata or {}).get('source'):
            from ..orchestrator import _agent_fetch_mapbox
            scene, _ = _agent_fetch_mapbox(a.bbox, basemap_source=a.metadata['source'])
            source = Path(settings.MEDIA_ROOT) / 'satellite_imgs' / scene.file_name
            if not source.is_file():
                raise ValueError('底图下载结果不存在')
            destination = _inside(_root() / 'files' / f'{a.id}.jpg')
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            a.scene = scene; a.file_path = str(destination); a.size_bytes = destination.stat().st_size
            a.metadata = {**(a.metadata or {}), 'acquired_at': None, 'source_path': str(source)}
        if not a.sha256:
            from .sandbox import file_hash
            a.sha256 = file_hash(_inside(a.file_path))
        _canonical(a)
        w, h, crs, transform, bbox, count = _raster_info(a.file_path)
        z = max(0, math.ceil(math.log2(max(w, h) / TILE_SIZE)))
        a.width, a.height, a.crs, a.transform = w, h, crs, transform
        a.bbox = a.bbox or bbox
        a.coordinate_space = 'geographic' if a.bbox else 'image_pixels'
        a.preview_path = _preview(a)
        a.metadata = {**(a.metadata or {}), 'tile_size': TILE_SIZE, 'tile_max_zoom': z,
                      'band_count': count, 'resolutions': [float(2 ** (z-k)) for k in range(z+1)]}
        # Sending a draft can attach it to a conversation during raster work.
        # Publish only processing-owned fields and only while holding this lease.
        fields = {name: getattr(a, name) for name in ('file_path', 'scene_id', 'size_bytes', 'sha256',
                  'width', 'height', 'crs', 'transform', 'bbox', 'coordinate_space', 'preview_path', 'metadata')}
        changed = SpatialAttachment.objects.filter(pk=a.pk, processing_claim=token).update(
            **fields, status='ready', error='', processing_claim='', processing_lease_until=None,
            updated_at=timezone.now())
        a.refresh_from_db()
        if not changed:
            return a
        if a.conversation_id:
            from .common import emit_event
            emit_event(a.conversation_id, 'attachment.ready', {'attachment': attachment_payload(a)})
    except Exception as e:
        SpatialAttachment.objects.filter(pk=a.pk, processing_claim=token).update(
            status='failed', error=str(e)[:1000], processing_claim='', processing_lease_until=None,
            updated_at=timezone.now())
        a.refresh_from_db()
    finally:
        stop.set()
        heartbeat.join(timeout=3)
    return a
def process_pending_assets(limit=2): return [process_attachment(i) for i in SpatialAttachment.objects.filter(status__in=['pending','processing']).order_by('created_at').values_list('id',flat=True)[:limit]]
@retry_sqlite_write
@transaction.atomic
def _create_observation(**values):
    return SpatialObservation.objects.create(**values)

def read_window(a,x,y,width,height,max_size=1536,observation_context=None):
    if a.status!='ready':raise ValueError('附件尚未处理完成')
    x,y,width,height,max_size=(int(v) for v in (x,y,width,height,max_size))
    if min(x,y)<0 or width<1 or height<1 or x+width>a.width or y+height>a.height or not 1<=max_size<=4096:raise ValueError('窗口参数无效')
    import rasterio, numpy as np
    from rasterio.enums import Resampling
    from rasterio.windows import Window
    scale=min(1,max_size/max(width,height)); size=(max(1,round(width*scale)),max(1,round(height*scale)))
    root = _root()
    t=_inside(root/'observations'/f'{uuid.uuid4()}.jpg', root); t.parent.mkdir(parents=True,exist_ok=True)
    with rasterio.open(a.file_path) as s:
        indexes = _display_indexes(a, s)
        d=s.read(indexes,window=Window(x,y,width,height),out_shape=(len(indexes),size[1],size[0]),resampling=Resampling.bilinear,masked=True)
    Image.fromarray(np.moveaxis(_rgb(d,(a.metadata or {}).get('display_stretch')),0,-1)).save(t,'JPEG',quality=88)
    extra = dict(observation_context or {})
    extra['metadata'] = {'output_size':list(size),'transform':a.transform, **extra.pop('metadata', {})}
    extra.setdefault('label', f'窗口 {x},{y}')
    o=_create_observation(conversation=a.conversation,attachment=a,window=[x,y,width,height],geometry={'type':'Polygon','coordinates':[[[x,y],[x+width,y],[x+width,y+height],[x,y+height],[x,y]]]},preview_path=str(t),**extra)
    return {'path':str(t),'window':o.window,'transform':a.transform,'observation':o}
def render_tile(a,z,x,y):
    """Render one tile directly; tile requests never create observations."""
    import rasterio, numpy as np
    from rasterio.enums import Resampling
    from rasterio.windows import Window
    maximum=int((a.metadata or {}).get('tile_max_zoom',0)); scale=2**(maximum-z)
    if not 0<=z<=maximum or x<0 or y<0: raise ValueError('瓦片坐标无效')
    left,top=x*TILE_SIZE*scale,y*TILE_SIZE*scale
    if left>=a.width or top>=a.height: raise ValueError('瓦片坐标无效')
    right,bottom=min(a.width,left+TILE_SIZE*scale),min(a.height,top+TILE_SIZE*scale)
    with rasterio.open(a.file_path) as src:
        indexes = _display_indexes(a, src)
        data=src.read(indexes,window=Window(left,top,right-left,bottom-top),out_shape=(len(indexes),math.ceil((bottom-top)/scale),math.ceil((right-left)/scale)),resampling=Resampling.bilinear,masked=True)
    image=Image.fromarray(np.moveaxis(_rgb(data,(a.metadata or {}).get('display_stretch')),0,-1)); import io; out=io.BytesIO(); image.save(out,'PNG'); return out.getvalue()
def attachment_payload(a):
    b=f'/api/v3/attachments/{a.id}'; return {'id':str(a.id),'name':a.name,'status':a.status,'kind':a.kind,'coordinate_space':a.coordinate_space,'width':a.width,'height':a.height,'bbox':a.bbox,'geometry':a.geometry,'crs':a.crs,'transform':a.transform,'metadata':a.metadata,'scene_id':a.scene_id,'preview_url':b+'/preview' if a.preview_path else None,'tile_url':b+'/tiles/{z}/{x}/{y}.png' if a.status=='ready' else None,'error':a.error or None}
def observation_payload(o): return {'id':str(o.id),'attachment_id':str(o.attachment_id),'label':o.label,'kind':o.kind,'window':o.window,'geometry':o.geometry,'summary':o.summary,'confidence':o.confidence,'evidence_refs':o.evidence_refs,'preview_url':f'/api/v3/observations/{o.id}/preview' if o.preview_path else None,'metadata':o.metadata}
