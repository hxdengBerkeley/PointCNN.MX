# coding: utf-8

import numpy as np
import mxnet as mx
import mxnet.gluon.nn as nn

# input: points(b, n, 3) idx(b, m)
# output: out(b, m, 3)
fwd_source = r'''
    __global__ void gatherpointKernel(int b,int n,int m
        ,const float * __restrict__ inp,const int * __restrict__ idx,float * __restrict__ out){
      for (int i=blockIdx.x;i<b;i+=gridDim.x){
        for (int j=blockIdx.y*blockDim.x+threadIdx.x;j<m;j+=blockDim.x*gridDim.y){
          int a=idx[i*m+j];
          out[(i*m+j)*3+0]=inp[(i*n+a)*3+0];
          out[(i*m+j)*3+1]=inp[(i*n+a)*3+1];
          out[(i*m+j)*3+2]=inp[(i*n+a)*3+2];
        }
      }
    }
'''

bwd_source = r'''
    __global__ void scatteraddpointKernel(int b,int n,int m
        ,const float * __restrict__ out_g,const int * __restrict__ idx,float * __restrict__ inp_g){
      for (int i=blockIdx.x;i<b;i+=gridDim.x){
        for (int j=blockIdx.y*blockDim.x+threadIdx.x;j<m;j+=blockDim.x*gridDim.y){
          int a=idx[i*m+j];
          atomicAdd(&inp_g[(i*n+a)*3+0],out_g[(i*m+j)*3+0]);
          atomicAdd(&inp_g[(i*n+a)*3+1],out_g[(i*m+j)*3+1]);
          atomicAdd(&inp_g[(i*n+a)*3+2],out_g[(i*m+j)*3+2]);
        }
      }
    }        
'''

fwd_module = mx.rtc.CudaModule(fwd_source, exports=['gatherpointKernel'])
fwd_kernel = fwd_module.get_kernel("gatherpointKernel"
                                        , "int b,int n,int m,const float * inp,const int * idx,float * out")

bwd_module = mx.rtc.CudaModule(bwd_source, exports=['scatteraddpointKernel'])
bwd_kernel = bwd_module.get_kernel("scatteraddpointKernel"
                                        , "int b,int n,int m,const float * out_g,const int * idx,float * inp_g")

class GatherPoint(mx.operator.CustomOp):
    def __init__(self):
        super(GatherPoint, self).__init__()

    def forward(self, is_train, req, in_data, out_data, aux):
        if req[0] == "null":
            return
        x = in_data[0]  # points
        idx = in_data[1] # idx
        B, N, _ = x.shape
        _, M = idx.shape
        y = mx.nd.empty(shape=(B, M, 3), ctx = x.context, dtype=np.float32) # output

        # args, ctx, grid_shape, block_shape, shared_mem = 0
        fwd_kernel.launch([B, N, M, x, idx, y], x.context, (2, 8, 1), (512, 1, 1))

        self.assign(out_data[0], req[0], y)

    def backward(self, req, out_grad, in_data, out_data, in_grad, aux):
        dx = mx.nd.empty(shape=in_data[0].shape, ctx = x.context, dtype=np.float32)
        
        bwd_kernel.launch([B, N, M, out_grad[0], in_data[1], dx], in_data[0].context, (2, 8, 1), (512, 1, 1))
        
        self.assign(in_grad[0], req[0], dx)

@mx.operator.register("GatherPoint")
class GatherPointProp(mx.operator.CustomOpProp):
    def __init__(self):
        super(GatherPointProp, self).__init__(need_top_grad=True)

    def list_arguments(self):
        return ['in_data', 'idx']

    def list_outputs(self):
        return ['output']

    def infer_shape(self, in_shape):
        output_shape = (in_shape[1][0], in_shape[1][1], 3)
        return in_shape, [output_shape], []

    def infer_type(self, in_type):
        return in_type, [np.float32], []

    def create_operator(self, ctx, in_shapes, in_dtypes):
        return GatherPoint()


# Input dataset: (b, n, 3), tmp: (b, n)
# Ouput idxs (b, m)
source = r'''
    __global__ void farthestpointsamplingKernel(int b,int n,int m
        ,const float * __restrict__ dataset,float * __restrict__ temp,int * __restrict__ idxs){
      if (m<=0)
        return;
      const int BlockSize=512;
      __shared__ float dists[BlockSize];
      __shared__ int dists_i[BlockSize];
      const int BufferSize=3072;
      __shared__ float buf[BufferSize*3];
      for (int i=blockIdx.x;i<b;i+=gridDim.x){
        int old=0;
        if (threadIdx.x==0)
          idxs[i*m+0]=old;
        for (int j=threadIdx.x;j<n;j+=blockDim.x){
          temp[blockIdx.x*n+j]=1e38;
        }
        for (int j=threadIdx.x;j<min(BufferSize,n)*3;j+=blockDim.x){
          buf[j]=dataset[i*n*3+j];
        }
        __syncthreads();
        for (int j=1;j<m;j++){
          int besti=0;
          float best=-1;
          float x1=dataset[i*n*3+old*3+0];
          float y1=dataset[i*n*3+old*3+1];
          float z1=dataset[i*n*3+old*3+2];
          for (int k=threadIdx.x;k<n;k+=blockDim.x){
            float td=temp[blockIdx.x*n+k];
            float x2,y2,z2;
            if (k<BufferSize){
              x2=buf[k*3+0];
              y2=buf[k*3+1];
              z2=buf[k*3+2];
            }else{
              x2=dataset[i*n*3+k*3+0];
              y2=dataset[i*n*3+k*3+1];
              z2=dataset[i*n*3+k*3+2];
            }
            float d=(x2-x1)*(x2-x1)+(y2-y1)*(y2-y1)+(z2-z1)*(z2-z1);
            float d2=min(d,td);
            if (d2!=td)
              temp[blockIdx.x*n+k]=d2;
            if (d2>best){
              best=d2;
              besti=k;
            }
          }
          dists[threadIdx.x]=best;
          dists_i[threadIdx.x]=besti;
          for (int u=0;(1<<u)<blockDim.x;u++){
            __syncthreads();
            if (threadIdx.x<(blockDim.x>>(u+1))){
              int i1=(threadIdx.x*2)<<u;
              int i2=(threadIdx.x*2+1)<<u;
              if (dists[i1]<dists[i2]){
                dists[i1]=dists[i2];
                dists_i[i1]=dists_i[i2];
              }
            }
          }
          __syncthreads();
          old=dists_i[0];
          if (threadIdx.x==0)
            idxs[i*m+j]=old;
        }
      }
    }
'''

module = mx.rtc.CudaModule(source, exports=['farthestpointsamplingKernel'])
kernel = module.get_kernel("farthestpointsamplingKernel", "int b,int n,int m,const float * dataset,float * temp,int * idxs")

class FarthestPointSampling(mx.operator.CustomOp):
    def __init__(self, npoints):
        super(FarthestPointSampling, self).__init__()
        self.npoints = npoints
    def forward(self, is_train, req, in_data, out_data, aux):
        if req[0] == "null":
            return
        x = in_data[0]  # input
        B, N, _ = x.shape
        tmp = mx.nd.ones(shape=(B, N), ctx = x.context) * 1e10
        y = mx.nd.empty(shape=(B, self.npoints), ctx = x.context, dtype=np.int32) # output

        # args, ctx, grid_shape, block_shape, shared_mem = 0
        kernel.launch([B, N, self.npoints, x, tmp, y], x.context, (32, 1, 1), (512, 1, 1))

        self.assign(out_data[0], req[0], y)

    def backward(self, req, out_grad, in_data, out_data, in_grad, aux):
        self.assign(in_grad[0], req[0], 0)

@mx.operator.register("FarthestPointSampling")
class FarthestPointSamplingProp(mx.operator.CustomOpProp):
    def __init__(self, npoints=0):
        super(FarthestPointSamplingProp, self).__init__(need_top_grad=False)
        
        self.npoints = int(npoints)

    def list_arguments(self):
        return ['in_data']

    def list_outputs(self):
        return ['output']

    def infer_shape(self, in_shape):
        data_shape = in_shape[0]
        output_shape = (in_shape[0][0], self.npoints)
        return [data_shape], [output_shape], []

    def infer_type(self, in_type):
        return in_type, [np.int32], []

    def create_operator(self, ctx, in_shapes, in_dtypes):
        return FarthestPointSampling(self.npoints)
