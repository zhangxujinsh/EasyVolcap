import math
import numpy as np
import torch
from torch import nn
from torch.nn import functional as F
from functools import partial

from easyvolcap.utils.net_utils import batch_rodrigues, torch_inverse_2x2
from easyvolcap.utils.net_utils import make_buffer, make_params
from easyvolcap.utils.sh_utils import eval_sh
from easyvolcap.utils.net_utils import normalize
from easyvolcap.utils.base_utils import dotdict


@torch.jit.script
def get_jacobian(pix_xyz: torch.Tensor,  # B, P, 3, point in screen space
                 ):
    J = pix_xyz.new_zeros(pix_xyz.shape + (3, ))  # B, P, 3, 3
    J[..., 0, 0] = 1 / pix_xyz[..., 2]
    J[..., 1, 1] = 1 / pix_xyz[..., 2]
    J[..., 0, 2] = -pix_xyz[..., 0] / pix_xyz[..., 2]**2
    J[..., 1, 2] = -pix_xyz[..., 1] / pix_xyz[..., 2]**2
    J[..., 2, 2] = 1
    return J


@torch.jit.script
def gaussian_2d(xy: torch.Tensor,  # B, H, W, 2, screen pixel locations for evaluation
                mean_xy: torch.Tensor,  # B, H, W, K, 2, center of the gaussian in screen space
                cov_xy: torch.Tensor,  # B, H, W, 2, 2, covariance of the gaussian in screen space
                # pow: float = 1,  # when pow != 1, not a real gaussian, but easier to control fall off
                # we want to the values at 3 sigma to zeros -> easier to control volume rendering?
                ):
    inv_cov_xy = torch_inverse_2x2(cov_xy)  # B, P, 2, 2
    minus_mean = xy[..., None, :] - mean_xy  # B, P, K, 2
    # weight = torch.exp(-0.5 * torch.einsum('...d,...de,...e->...', x_minus_mean, inv_cov_xy, x_minus_mean))  # B, H, W, K
    xTsigma_new = (minus_mean[..., None] * inv_cov_xy[..., None, :, :]).sum(dim=-2)  # B, P, K, 2
    xTsigma_x = (xTsigma_new * minus_mean).sum(dim=-1)  # B, P, K
    return xTsigma_x


@torch.jit.script
def gaussian_3d(scale3: torch.Tensor,  # B, P, 3, the scale of the 3d gaussian in 3 dimensions
                rot3: torch.Tensor,  # B, P, 3, the rotation of the 3D gaussian (angle-axis)
                R: torch.Tensor,  # B, 3, 3, camera rotation
                ):
    sigma0 = torch.diag_embed(scale3)  # B, P, 3, 3
    rotmat = batch_rodrigues(rot3)  # B, P, 3, 3
    R_sigma = rotmat @ sigma0
    covariance = R @ R_sigma @ R_sigma.mT @ R.mT
    return covariance  # B, P, 3, 3


@torch.jit.script
def RGB2SH(rgb):
    C0 = 0.28209479177387814
    return (rgb - 0.5) / C0


@torch.jit.script
def SH2RGB(sh):
    C0 = 0.28209479177387814
    return sh * C0 + 0.5


@torch.jit.script
def inverse_sigmoid(x):
    return torch.log(x / (1 - x))


def strip_lowerdiag(L: torch.Tensor):
    uncertainty = torch.zeros((L.shape[0], 6), dtype=torch.float, device=L.device)

    uncertainty[:, 0] = L[:, 0, 0]
    uncertainty[:, 1] = L[:, 0, 1]
    uncertainty[:, 2] = L[:, 0, 2]
    uncertainty[:, 3] = L[:, 1, 1]
    uncertainty[:, 4] = L[:, 1, 2]
    uncertainty[:, 5] = L[:, 2, 2]
    return uncertainty


def strip_symmetric(sym):
    return strip_lowerdiag(sym)


def build_rotation(r: torch.Tensor):
    norm = torch.sqrt(r[:, 0] * r[:, 0] + r[:, 1] * r[:, 1] + r[:, 2] * r[:, 2] + r[:, 3] * r[:, 3])

    q = r / norm[:, None]

    R = torch.zeros((q.size(0), 3, 3), device=r.device)

    r = q[:, 0]
    x = q[:, 1]
    y = q[:, 2]
    z = q[:, 3]

    R[:, 0, 0] = 1 - 2 * (y * y + z * z)
    R[:, 0, 1] = 2 * (x * y - r * z)
    R[:, 0, 2] = 2 * (x * z + r * y)
    R[:, 1, 0] = 2 * (x * y + r * z)
    R[:, 1, 1] = 1 - 2 * (x * x + z * z)
    R[:, 1, 2] = 2 * (y * z - r * x)
    R[:, 2, 0] = 2 * (x * z - r * y)
    R[:, 2, 1] = 2 * (y * z + r * x)
    R[:, 2, 2] = 1 - 2 * (x * x + y * y)
    return R


def build_scaling_rotation(s: torch.Tensor, r: torch.Tensor):
    L = torch.zeros((s.shape[0], 3, 3), dtype=torch.float, device=s.device)
    R = build_rotation(r)

    L[:, 0, 0] = s[:, 0]
    L[:, 1, 1] = s[:, 1]
    L[:, 2, 2] = s[:, 2]

    L = R @ L
    return L


@torch.jit.script
def fov2focal(fov, pixels):
    return pixels / (2 * math.tan(fov / 2))


@torch.jit.script
def focal2fov(focal, pixels):
    return 2 * math.atan(pixels / (2 * focal))


def getWorld2View(R: torch.Tensor, t: torch.Tensor):
    """
    R: ..., 3, 3
    T: ..., 3, 1
    """
    sh = R.shape[:-2]
    T = torch.zeros((*sh, 4, 4), dtype=R.dtype, device=R.device)
    T[..., :3, :3] = R
    T[..., :3, 3:] = t
    T[..., 3, 3] = 1.0
    return T


def getProjectionMatrix(K: torch.Tensor, H, W, znear=0.001, zfar=1000):
    fx = K[0, 0]
    fy = K[1, 1]
    cx = K[0, 2]
    cy = K[1, 2]
    s = K[0, 1]

    P = torch.zeros(4, 4, dtype=K.dtype, device=K.device)

    z_sign = 1.0

    P[0, 0] = 2 * fx / W
    P[0, 1] = 2 * s / W
    P[0, 2] = -1 + 2 * (cx / W)

    P[1, 1] = 2 * fy / H
    P[1, 2] = -1 + 2 * (cy / H)

    P[2, 2] = z_sign * (zfar + znear) / (zfar - znear)
    P[2, 3] = z_sign * 2 * zfar * znear / (zfar - znear)
    P[3, 2] = z_sign

    return P


def convert_to_gaussian_camera(K: torch.Tensor, R: torch.Tensor, T: torch.Tensor,
                               H: torch.Tensor, W: torch.Tensor,
                               znear=0.01, zfar=100.):
    output = dotdict()

    output.image_height = H
    output.image_width = W

    output.K = K
    output.R = R
    output.T = T

    fl_x = K[0, 0]
    fl_y = K[1, 1]

    output.FoVx = focal2fov(fl_x, output.image_width)
    output.FoVy = focal2fov(fl_y, output.image_height)

    output.world_view_transform = getWorld2View(output.R, output.T).transpose(0, 1)
    output.projection_matrix = getProjectionMatrix(output.K, output.image_height, output.image_width, znear, zfar).transpose(0, 1)
    output.full_proj_transform = torch.matmul(output.world_view_transform,
                                              output.projection_matrix)
    output.camera_center = output.world_view_transform.inverse()[3:, :3]

    return output


class GaussianModel(nn.Module):
    def setup_functions(self):
        def build_covariance_from_scaling_rotation(scaling, scaling_modifier, rotation):
            L = build_scaling_rotation(scaling_modifier * scaling, rotation)
            actual_covariance = L @ L.transpose(1, 2)
            symm = strip_symmetric(actual_covariance)
            return symm

        self.scaling_activation = torch.exp
        self.scaling_inverse_activation = torch.log

        self.covariance_activation = build_covariance_from_scaling_rotation

        self.opacity_activation = torch.sigmoid
        self.inverse_opacity_activation = inverse_sigmoid

        self.rotation_activation = F.normalize

    def __init__(self,
                 xyz: torch.Tensor,
                 colors: torch.Tensor,
                 init_occ: float = 0.1,
                 sh_deg: int = 3,
                 pcd_embedder: nn.Module = None,
                 resd_regressor: nn.Module = None,
                 xyz_embedder: nn.Module = None,
                 gaussian_regressor: nn.Module = None,
                 ibr_embedder: nn.Module = None,
                 ibr_regressor: nn.Module = None,
                 frame: int = 0,
                 ):
        super().__init__()
        self.setup_functions()
        
        self.pcd_embedder = pcd_embedder
        self.resd_regressor = resd_regressor
        self.xyz_embedder = xyz_embedder
        self.gaussian_regressor = gaussian_regressor
        self.ibr_embedder = ibr_embedder
        self.ibr_regressor = ibr_regressor
        self.f = frame

        # sh realte configs
        self.active_sh_degree = make_buffer(torch.zeros(1))
        self.max_sh_degree = sh_deg

        # initalize trainable parameters
        self.create_from_pcd(xyz, colors, init_occ)

        # densification related parameters
        self.max_radii2D = make_buffer(torch.zeros(self._xyz.shape[0]))
        self.xyz_gradient_accum = make_buffer(torch.zeros((self._xyz.shape[0], 1)))
        self.denom = make_buffer(torch.zeros((self._xyz.shape[0], 1)))

    @property
    def get_scaling(self):
        pcd = self._xyz
        pcd_t = torch.ones((self._xyz.shape[0], 1), dtype=torch.float, device='cuda') * self.f
        pcd_feat = self.pcd_embedder(pcd, pcd_t)  # B, N, C
        resd = self.resd_regressor(pcd_feat)  # B, N, 3
        xyz = pcd + resd  # B, N, 3
        xyz_feat = self.xyz_embedder(xyz, pcd_t)  # same time
        # These could be stored
        scale3, rot4, alpha = self.gaussian_regressor(xyz_feat)  # B, N, 1
        return scale3

    @property
    def get_rotation(self):
        pcd = self._xyz
        pcd_t = torch.ones((self._xyz.shape[0], 1), dtype=torch.float, device='cuda') * self.f
        pcd_feat = self.pcd_embedder(pcd, pcd_t)  # B, N, C
        resd = self.resd_regressor(pcd_feat)  # B, N, 3
        xyz = pcd + resd  # B, N, 3
        xyz_feat = self.xyz_embedder(xyz, pcd_t)  # same time
        # These could be stored
        scale3, rot4, alpha = self.gaussian_regressor(xyz_feat)  # B, N, 1
        return rot4    
    
    @property
    def get_bunch(self):
        pcd = self._xyz
        pcd_t = torch.ones((self._xyz.shape[0], 1), dtype=torch.float, device='cuda') * self.f
        pcd_feat = self.pcd_embedder(pcd, pcd_t)  # B, N, C
        resd = self.resd_regressor(pcd_feat)  # B, N, 3
        xyz = pcd + resd  # B, N, 3
        xyz_feat = self.xyz_embedder(xyz, pcd_t)  # same time
        # These could be stored
        scale3, rot4, alpha = self.gaussian_regressor(xyz_feat)  # B, N, 1
        return scale3, rot4, alpha
    
    @property
    def get_xyz(self):
        # raise NotImplementedError()
        return self._xyz

    @property
    def get_features(self):
        raise NotImplementedError()
        features_dc = self._features_dc
        features_rest = self._features_rest
        return torch.cat((features_dc, features_rest), dim=1)

    @property
    def get_opacity(self):
        pcd = self._xyz
        pcd_t = torch.ones((self._xyz.shape[0], 1), dtype=torch.float, device='cuda') * self.f
        pcd_feat = self.pcd_embedder(pcd, pcd_t)  # B, N, C
        resd = self.resd_regressor(pcd_feat)  # B, N, 3
        xyz = pcd + resd  # B, N, 3
        xyz_feat = self.xyz_embedder(xyz, pcd_t)  # same time
        # These could be stored
        scale3, rot4, alpha = self.gaussian_regressor(xyz_feat)  # B, N, 1
        return alpha

    def get_covariance(self, scaling, rotation, scaling_modifier=1):
        return self.covariance_activation(scaling, scaling_modifier, rotation)

    def oneupSHdegree(self):
        if self.active_sh_degree < self.max_sh_degree:
            self.active_sh_degree += 1

    def create_from_pcd(self, xyz: torch.Tensor, colors: torch.Tensor, opacity: float = 0.1):
        # from simple_knn._C import distCUDA2

        # features = torch.zeros((xyz.shape[0], 3, (self.max_sh_degree + 1) ** 2))
        # if colors is not None:
        #     SH = RGB2SH(colors)
        #     features[:, :3, 0] = SH
        # features[:, 3: 1:] = 0

        # dist2 = torch.clamp_min(distCUDA2(xyz.float().cuda()), 0.0000001)
        # scales = torch.log(torch.sqrt(dist2))[..., None].repeat(1, 3)
        # rots = torch.zeros((xyz.shape[0], 4))
        # rots[:, 0] = 1

        # opacities = inverse_sigmoid(opacity * torch.ones((xyz.shape[0], 1), dtype=torch.float))

        self._xyz = make_params(xyz)
        # self._features_dc = make_params(features[:, :, :1].transpose(1, 2).contiguous())
        # self._features_rest = make_params(features[:, :, 1:].transpose(1, 2).contiguous())
        # self._scaling = make_params(scales)
        # self._rotation = make_params(rots)
        # self._opacity = make_params(opacities)
    
    def reset_opacity(self, optimizer_state):
        raise NotImplementedError()
        for _, val in optimizer_state.items():
            if val.name == '_opacity':
                break
        opacities_new = inverse_sigmoid(torch.min(self.get_opacity, torch.ones_like(self.get_opacity)*0.01))
        self._opacity.set_(opacities_new.detach())
        self._opacity.grad = None
        val.old_keep = torch.zeros_like(val.old_keep, dtype=torch.bool)
        val.new_keep = torch.zeros_like(val.new_keep, dtype=torch.bool)
        val.new_params = self._opacity
        # optimizable_tensors = self.replace_tensor_to_optimizer(opacities_new, "opacity")
        # self._opacity = optimizable_tensors["opacity"]

    def replace_tensor_to_optimizer(self, tensor, name):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            if group["name"] == name:
                stored_state = self.optimizer.state.get(group['params'][0], None)
                stored_state["exp_avg"] = torch.zeros_like(tensor)
                stored_state["exp_avg_sq"] = torch.zeros_like(tensor)

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter(tensor.requires_grad_(True))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
        return optimizable_tensors

    def _prune_optimizer(self, mask: torch.Tensor):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            stored_state = self.optimizer.state.get(group['params'][0], None)
            if stored_state is not None:
                stored_state["exp_avg"] = stored_state["exp_avg"][mask]
                stored_state["exp_avg_sq"] = stored_state["exp_avg_sq"][mask]

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter((group["params"][0][mask].requires_grad_(True)))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
            else:
                group["params"][0] = nn.Parameter(group["params"][0][mask].requires_grad_(True))
                optimizable_tensors[group["name"]] = group["params"][0]
        return optimizable_tensors
        
    def prune_points(self, mask):
        valid_points_mask = ~mask
        # optimizable_tensors = self._prune_optimizer(valid_points_mask)

        # self._xyz = optimizable_tensors["xyz"]
        # self._features_dc = optimizable_tensors["f_dc"]
        # self._features_rest = optimizable_tensors["f_rest"]
        # self._opacity = optimizable_tensors["opacity"]
        # self._scaling = optimizable_tensors["scaling"]
        # self._rotation = optimizable_tensors["rotation"]

        self._xyz.set_(self._xyz[valid_points_mask].detach())
        self._xyz.grad = None
        # self._features_dc.set_(self._features_dc[valid_points_mask].detach())
        # self._features_dc.grad = None
        # self._features_rest.set_(self._features_rest[valid_points_mask].detach())
        # self._features_rest.grad = None
        # self._opacity.set_(self._opacity[valid_points_mask].detach())
        # self._opacity.grad = None
        # self._scaling.set_(self._scaling[valid_points_mask].detach())
        # self._scaling.grad = None
        # self._rotation.set_(self._rotation[valid_points_mask].detach())
        # self._rotation.grad = None

        self.xyz_gradient_accum.set_(self.xyz_gradient_accum[valid_points_mask])
        self.xyz_gradient_accum.grad = None
        self.denom.set_(self.denom[valid_points_mask])
        self.denom.grad = None
        self.max_radii2D.set_(self.max_radii2D[valid_points_mask])
        self.max_radii2D.grad = None

    def cat_tensors_to_optimizer(self, tensors_dict):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            assert len(group["params"]) == 1
            extension_tensor = tensors_dict[group["name"]]
            stored_state = self.optimizer.state.get(group['params'][0], None)
            if stored_state is not None:

                stored_state["exp_avg"] = torch.cat((stored_state["exp_avg"], torch.zeros_like(extension_tensor)), dim=0)
                stored_state["exp_avg_sq"] = torch.cat((stored_state["exp_avg_sq"], torch.zeros_like(extension_tensor)), dim=0)

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter(torch.cat((group["params"][0], extension_tensor), dim=0).requires_grad_(True))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
            else:
                group["params"][0] = nn.Parameter(torch.cat((group["params"][0], extension_tensor), dim=0).requires_grad_(True))
                optimizable_tensors[group["name"]] = group["params"][0]

        return optimizable_tensors

    def densification_postfix(self, new_xyz, optimizer_state):
        d = dotdict({
            "_xyz": new_xyz,
        })

        # optimizable_tensors = self.cat_tensors_to_optimizer(d)
        # self._xyz = optimizable_tensors["xyz"]
        # self._features_dc = optimizable_tensors["f_dc"]
        # self._features_rest = optimizable_tensors["f_rest"]
        # self._opacity = optimizable_tensors["opacity"]
        # self._scaling = optimizable_tensors["scaling"]
        # self._rotation = optimizable_tensors["rotation"]

        for name, new_params in d.items():
            params: nn.Parameter = getattr(self, name)
            params.set_(torch.cat((params.data, new_params), dim=0).detach())
            params.grad = None

        device = self.get_xyz.device
        self.xyz_gradient_accum.set_(torch.zeros((self.get_xyz.shape[0], 1), device=device))
        self.xyz_gradient_accum.grad = None
        self.denom.set_(torch.zeros((self.get_xyz.shape[0], 1), device=device))
        self.denom.grad = None
        self.max_radii2D.set_(torch.zeros((self.get_xyz.shape[0]), device=device))
        self.max_radii2D.grad = None

        for val in optimizer_state.values():
            name = val.name
            val.new_keep = torch.cat((val.new_keep, torch.zeros_like(d[name], dtype=torch.bool, requires_grad=False)), dim=0)
            val.new_params = getattr(self, name)
            assert val.new_keep.shape == val.new_params.shape
        
    def densify_and_split(self, grads, grad_threshold, scene_extent, percent_dense, min_opacity, max_screen_size, optimizer_state, N=2):
        n_init_points = self.get_xyz.shape[0]
        device = self.get_xyz.device
        
        scaling, rotation, alpha = self.get_bunch
    
        # Extract points that satisfy the gradient condition
        padded_grad = torch.zeros((n_init_points), device=device)
        padded_grad[:grads.shape[0]] = grads.squeeze()
        selected_pts_mask = padded_grad >= grad_threshold
        selected_pts_mask = torch.logical_and(selected_pts_mask,
                                              torch.max(scaling, dim=1).values > percent_dense*scene_extent)

        stds = scaling[selected_pts_mask].repeat(N,1)
        means =torch.zeros((stds.size(0), 3), device=device)
        samples = torch.normal(mean=means, std=stds)
        rots = build_rotation(rotation[selected_pts_mask]).repeat(N,1,1)
        new_xyz = torch.bmm(rots, samples.unsqueeze(-1)).squeeze(-1) + self.get_xyz[selected_pts_mask].repeat(N, 1)
        # new_scaling = self.scaling_inverse_activation(scaling[selected_pts_mask].repeat(N,1) / (0.8*N))
        # new_rotation = rotation[selected_pts_mask].repeat(N,1)
        # new_features_dc = self._features_dc[selected_pts_mask].repeat(N,1,1)
        # new_features_rest = self._features_rest[selected_pts_mask].repeat(N,1,1)
        # new_opacity = self._opacity[selected_pts_mask].repeat(N,1)

        self.densification_postfix(new_xyz, optimizer_state)

        prune_filter = torch.cat((selected_pts_mask, torch.zeros(N * selected_pts_mask.sum(), device=device, dtype=bool)))
        self.prune_points(prune_filter)
        old_keep_mask = ~prune_filter[:grads.shape[0]]
        for val in optimizer_state.values():
            name = val.name
            val.old_keep[~old_keep_mask] = False
            val.new_keep = val.new_keep[~prune_filter]
            val.params = getattr(self, name)
            assert val.old_keep.sum() == val.new_keep.sum()
            assert val.new_keep.shape == val.new_params.shape
        
        prune_mask = (self.get_opacity < min_opacity).squeeze()
        if max_screen_size:
            big_points_vs = self.max_radii2D > max_screen_size
            big_points_ws = self.get_scaling.max(dim=1).values > 0.1 * scene_extent
            prune_mask = torch.logical_or(torch.logical_or(prune_mask, big_points_vs), big_points_ws)
        self.prune_points(prune_mask)
        _old_keep_mask = old_keep_mask.clone()
        mask_mask = old_keep_mask[old_keep_mask]
        _mask = prune_mask[:mask_mask.shape[0]]
        mask_mask[_mask] = False
        old_keep_mask[_old_keep_mask] = mask_mask
        for val in optimizer_state.values():
            name = val.name
            val.old_keep[~old_keep_mask] = False
            val.new_keep = val.new_keep[~prune_mask]
            val.params = getattr(self, name)
            assert val.old_keep.sum() == val.new_keep.sum()
            assert val.new_keep.shape == val.new_params.shape

    def densify_and_clone(self, grads, grad_threshold, scene_extent, percent_dense, optimizer_state):
        # Extract points that satisfy the gradient condition
        selected_pts_mask = torch.norm(grads, dim=-1) >= grad_threshold
        selected_pts_mask = torch.logical_and(selected_pts_mask,
                                              torch.max(self.get_scaling, dim=1).values <= percent_dense*scene_extent)
        
        new_xyz = self._xyz[selected_pts_mask]
        # new_features_dc = self._features_dc[selected_pts_mask]
        # new_features_rest = self._features_rest[selected_pts_mask]
        # new_opacities = self._opacity[selected_pts_mask]
        # new_scaling = self._scaling[selected_pts_mask]
        # new_rotation = self._rotation[selected_pts_mask]

        self.densification_postfix(new_xyz, optimizer_state)

    def densify_and_prune(self, max_grad, min_opacity, extent, max_screen_size, percent_dense, optimizer_state):
        
        grads = self.xyz_gradient_accum / self.denom
        grads[grads.isnan()] = 0.0

        self.densify_and_clone(grads, max_grad, extent, percent_dense, optimizer_state)
        self.densify_and_split(grads, max_grad, extent, percent_dense, min_opacity, max_screen_size, optimizer_state)

        torch.cuda.empty_cache()

    def add_densification_stats(self, viewspace_point_tensor, update_filter):
        self.xyz_gradient_accum[update_filter] += torch.norm(viewspace_point_tensor.grad[update_filter,:2], dim=-1, keepdim=True)
        self.denom[update_filter] += 1

    def construct_list_of_attributes(self):
        l = ['x', 'y', 'z', 'nx', 'ny', 'nz']
        # All channels except the 3 DC
        for i in range(self._features_dc.shape[1]*self._features_dc.shape[2]):
            l.append('f_dc_{}'.format(i))
        for i in range(self._features_rest.shape[1]*self._features_rest.shape[2]):
            l.append('f_rest_{}'.format(i))
        l.append('opacity')
        for i in range(self._scaling.shape[1]):
            l.append('scale_{}'.format(i))
        for i in range(self._rotation.shape[1]):
            l.append('rot_{}'.format(i))
        return l
    
    def save_ply(self, path):
        import os
        from plyfile import PlyData, PlyElement
        dirname = os.path.dirname(path)
        os.makedirs(dirname, exist_ok=True)
        
        xyz = self._xyz.detach().cpu().numpy()
        normals = np.zeros_like(xyz)
        f_dc = self._features_dc.detach().transpose(1, 2).flatten(start_dim=1).contiguous().cpu().numpy()
        f_rest = self._features_rest.detach().transpose(1, 2).flatten(start_dim=1).contiguous().cpu().numpy()
        opacities = self._opacity.detach().cpu().numpy()
        scale = self._scaling.detach().cpu().numpy()
        rotation = self._rotation.detach().cpu().numpy()

        dtype_full = [(attribute, 'f4') for attribute in self.construct_list_of_attributes()]

        elements = np.empty(xyz.shape[0], dtype=dtype_full)
        attributes = np.concatenate((xyz, normals, f_dc, f_rest, opacities, scale, rotation), axis=1)
        elements[:] = list(map(tuple, attributes))
        el = PlyElement.describe(elements, 'vertex')
        PlyData([el]).write(path)
        
    def get_render_params(self, batch):
        pc = dotdict()
        
        pcd = self._xyz
        pcd_t = torch.ones((self._xyz.shape[0], 1), dtype=torch.float, device='cuda') * self.f
        pcd_feat = self.pcd_embedder(pcd, pcd_t)  # B, N, C
        resd = self.resd_regressor(pcd_feat)  # B, N, 3
        xyz = pcd + resd  # B, N, 3
        pc.get_xyz = xyz
        
        xyz_feat = self.xyz_embedder(xyz, pcd_t)  # same time
        # These could be stored
        scale3, rot4, alpha = self.gaussian_regressor(xyz_feat)  # B, N, 1
        src_feat = self.ibr_embedder(xyz[None], batch)  # MARK: implicit update of batch.output
        dir = normalize(xyz.detach() - (-batch.R.mT @ batch.T).mT)  # B, N, 3
        rgb = self.ibr_regressor(torch.cat([xyz_feat[None], dir], dim=-1), batch)
        
        pc.active_sh_degree = self.active_sh_degree
        pc.max_sh_degree = self.max_sh_degree
        pc.get_opacity = alpha
        pc.get_covariance = partial(self.get_covariance, scale3, rot4)
        pc.get_scaling = scale3
        pc.get_rotation = rot4
        pc.get_features = None
        pc.override_color = rgb[0]
        return pc
        

def render(viewpoint_camera, gm: GaussianModel, pipe, bg_color: torch.Tensor, scaling_modifier=1.0, override_color=None, batch=None):
    """
    Render the scene. 

    Background tensor (bg_color) must be on GPU!
    """
    pc: dotdict = gm.get_render_params(batch)
    override_color = pc.override_color

    from diff_gauss import GaussianRasterizationSettings, GaussianRasterizer

    # Create zero tensor. We will use it to make pytorch return gradients of the 2D (screen-space) means
    screenspace_points = torch.zeros_like(pc.get_xyz, dtype=pc.get_xyz.dtype, requires_grad=True, device=pc.get_xyz.device) + 0
    try:
        screenspace_points.retain_grad()
    except:
        pass

    # Set up rasterization configuration
    tanfovx = math.tan(viewpoint_camera.FoVx * 0.5)
    tanfovy = math.tan(viewpoint_camera.FoVy * 0.5)

    raster_settings = GaussianRasterizationSettings(
        image_height=int(viewpoint_camera.image_height),
        image_width=int(viewpoint_camera.image_width),
        tanfovx=tanfovx,
        tanfovy=tanfovy,
        bg=bg_color,
        scale_modifier=scaling_modifier,
        viewmatrix=viewpoint_camera.world_view_transform,
        projmatrix=viewpoint_camera.full_proj_transform,
        sh_degree=pc.active_sh_degree,
        campos=viewpoint_camera.camera_center,
        prefiltered=False,
        debug=pipe.debug
    )

    rasterizer = GaussianRasterizer(raster_settings=raster_settings)

    means3D = pc.get_xyz
    means2D = screenspace_points
    opacity = pc.get_opacity

    # If precomputed 3d covariance is provided, use it. If not, then it will be computed from
    # scaling / rotation by the rasterizer.
    scales = None
    rotations = None
    cov3D_precomp = None
    if pipe.compute_cov3D_python:
        cov3D_precomp = pc.get_covariance(scaling_modifier)
    else:
        scales = pc.get_scaling
        rotations = pc.get_rotation

    # If precomputed colors are provided, use them. Otherwise, if it is desired to precompute colors
    # from SHs in Python, do it. If not, then SH -> RGB conversion will be done by rasterizer.
    shs = None
    colors_precomp = None
    if override_color is None:
        if pipe.convert_SHs_python:
            shs_view = pc.get_features.transpose(1, 2).view(-1, 3, (pc.max_sh_degree + 1)**2)
            dir_pp = (pc.get_xyz - viewpoint_camera.camera_center.repeat(pc.get_features.shape[0], 1))
            dir_pp_normalized = dir_pp / dir_pp.norm(dim=1, keepdim=True)
            sh2rgb = eval_sh(pc.active_sh_degree, shs_view, dir_pp_normalized)
            colors_precomp = torch.clamp_min(sh2rgb + 0.5, 0.0)
        else:
            shs = pc.get_features
    else:
        colors_precomp = override_color

    # Rasterize visible Gaussians to image, obtain their radii (on screen).
    # breakpoint()
    rendered_image, radii = rasterizer(
        means3D=means3D,
        means2D=means2D,
        shs=shs,
        colors_precomp=colors_precomp,
        opacities=opacity,
        scales=scales,
        rotations=rotations,
        cov3D_precomp=cov3D_precomp)

    # Those Gaussians that were frustum culled or had a radius of 0 were not visible.
    # They will be excluded from value updates used in the splitting criteria.
    return dotdict({
        "render": rendered_image,
        # "alpha": rendered_image[3:],
        # "depth": rendered_depth,
        "viewspace_points": screenspace_points,
        "visibility_filter": radii > 0,
        "radii": radii
    })


def naive_render(viewpoint_camera, pc: GaussianModel, pipe, bg_color: torch.Tensor, scaling_modifier=1.0, override_color=None):
    """
    Render the scene. 

    Background tensor (bg_color) must be on GPU!
    """

    from diff_gauss import GaussianRasterizationSettings, GaussianRasterizer

    # Create zero tensor. We will use it to make pytorch return gradients of the 2D (screen-space) means
    screenspace_points = torch.zeros_like(pc.get_xyz, dtype=pc.get_xyz.dtype, requires_grad=True, device=pc.get_xyz.device) + 0
    try:
        screenspace_points.retain_grad()
    except:
        pass

    # Set up rasterization configuration
    tanfovx = math.tan(viewpoint_camera.FoVx * 0.5)
    tanfovy = math.tan(viewpoint_camera.FoVy * 0.5)

    raster_settings = GaussianRasterizationSettings(
        image_height=int(viewpoint_camera.image_height),
        image_width=int(viewpoint_camera.image_width),
        tanfovx=tanfovx,
        tanfovy=tanfovy,
        bg=bg_color,
        scale_modifier=scaling_modifier,
        viewmatrix=viewpoint_camera.world_view_transform,
        projmatrix=viewpoint_camera.full_proj_transform,
        sh_degree=pc.active_sh_degree,
        campos=viewpoint_camera.camera_center,
        prefiltered=False,
        debug=pipe.debug
    )

    rasterizer = GaussianRasterizer(raster_settings=raster_settings)

    means3D = pc.get_xyz
    means2D = screenspace_points
    opacity = pc.get_opacity

    # If precomputed 3d covariance is provided, use it. If not, then it will be computed from
    # scaling / rotation by the rasterizer.
    scales = None
    rotations = None
    cov3D_precomp = None
    if pipe.compute_cov3D_python:
        cov3D_precomp = pc.get_covariance(scaling_modifier)
    else:
        scales = pc.get_scaling
        rotations = pc.get_rotation

    # If precomputed colors are provided, use them. Otherwise, if it is desired to precompute colors
    # from SHs in Python, do it. If not, then SH -> RGB conversion will be done by rasterizer.
    shs = None
    colors_precomp = None
    if override_color is None:
        if pipe.convert_SHs_python:
            shs_view = pc.get_features.transpose(1, 2).view(-1, 3, (pc.max_sh_degree + 1)**2)
            dir_pp = (pc.get_xyz - viewpoint_camera.camera_center.repeat(pc.get_features.shape[0], 1))
            dir_pp_normalized = dir_pp / dir_pp.norm(dim=1, keepdim=True)
            sh2rgb = eval_sh(pc.active_sh_degree, shs_view, dir_pp_normalized)
            colors_precomp = torch.clamp_min(sh2rgb + 0.5, 0.0)
        else:
            shs = pc.get_features
    else:
        colors_precomp = override_color

    # Rasterize visible Gaussians to image, obtain their radii (on screen).
    # rendered_image, radii, rendered_depth = rasterizer(
    rendered_image, radii = rasterizer(
        means3D=means3D,
        means2D=means2D,
        shs=shs,
        colors_precomp=colors_precomp,
        opacities=opacity,
        scales=scales,
        rotations=rotations,
        cov3D_precomp=cov3D_precomp)

    raster_settings = GaussianRasterizationSettings(
        image_height=int(viewpoint_camera.image_height),
        image_width=int(viewpoint_camera.image_width),
        tanfovx=tanfovx,
        tanfovy=tanfovy,
        bg=torch.zeros_like(bg_color),
        scale_modifier=scaling_modifier,
        viewmatrix=viewpoint_camera.world_view_transform,
        projmatrix=viewpoint_camera.full_proj_transform,
        sh_degree=pc.active_sh_degree,
        campos=viewpoint_camera.camera_center,
        prefiltered=False,
        debug=pipe.debug
    )
    rasterizer.raster_settings = raster_settings

    colors_precomp = torch.ones_like(means3D, requires_grad=False).contiguous()
    rendered_alpha, _ = rasterizer(
        means3D=means3D,
        means2D=means2D,
        shs=None,
        colors_precomp=colors_precomp,
        opacities=opacity,
        scales=scales,
        rotations=rotations,
        cov3D_precomp=cov3D_precomp)

    colors_precomp = F.pad(means3D, (0, 1), value=1.0) @ viewpoint_camera.world_view_transform
    colors_precomp = torch.norm(colors_precomp[..., :3] - viewpoint_camera.camera_center, dim=-1, keepdim=True)
    colors_precomp = torch.repeat_interleave(colors_precomp, 3, dim=-1).contiguous()
    rendered_depth, _ = rasterizer(
        means3D=means3D,
        means2D=means2D,
        shs=None,
        colors_precomp=colors_precomp,
        opacities=opacity,
        scales=scales,
        rotations=rotations,
        cov3D_precomp=cov3D_precomp)

    # Those Gaussians that were frustum culled or had a radius of 0 were not visible.
    # They will be excluded from value updates used in the splitting criteria.
    return dotdict({
        "render": rendered_image[:3],
        "alpha": rendered_alpha[:1],
        "depth": rendered_depth[:1],
        "viewspace_points": screenspace_points,
        "visibility_filter": radii > 0,
        "radii": radii
    })
