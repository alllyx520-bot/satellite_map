import {describe,it,expect} from 'vitest';
import {geographicView,projectView} from './geo';
import type {Attachment} from './types';

const a:Attachment={id:'first',name:'first.tif',status:'ready',kind:'geotiff',coordinate_space:'geographic',width:20000,height:20000,
  crs:'EPSG:32650', transform:[10,0,420000,0,-10,4420000],metadata:{}};
describe('geographic comparison',()=>{
  it('keeps the same ground position when two source grids differ',()=>{
    const b={...a,id:'second',transform:[20,0,421000,0,-20,4419000]};
    const geo=geographicView(a,[500,-700],1.5,.23)!;
    const result=projectView(b,geo)!;
    expect(result.center[0]).toBeCloseTo(200,5);
    expect(result.center[1]).toBeCloseTo(-300,5);
    expect(result.resolution).toBeCloseTo(.75,5);
    expect(result.rotation).toBeCloseTo(.23,5);
  });
  it('round trips rotated affine coordinates within one original pixel',()=>{
    const rotated={...a,transform:[10,1,420000,-2,-10,4420000]};
    const result=projectView(rotated,geographicView(rotated,[712.35,-853.8],2.7,-.4))!;
    expect(result.center[0]).toBeCloseTo(712.35,5);
    expect(result.center[1]).toBeCloseTo(-853.8,5);
    expect(result.resolution).toBeCloseTo(2.7,5);
  });
  it('does not invent geographic coordinates for a plain image',()=>{
    expect(geographicView({...a,coordinate_space:'image_pixels'},[500,-700],1,0)).toBeUndefined();
  });
});
