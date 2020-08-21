'''
    use image wrapping to further reduce the calculation time.
    2D DWT is used to get the multi-resolution images.
'''
'''
    use speckle step scanning data to calculate the pixel movement.
    The data is a sequence of images, and every two image the movement can be calculated.
'''
import numpy as np
import pywt
import os
import sys
import time
# import tifffile as tiff
from PIL import Image
import glob
import scipy.constants as sc
from func import prColor, frankotchellappa, image_roi, Wavelet_transform, write_h5, write_json, filter_erosion, find_disp, find_disp_2nd
from euclidean_dist import dist_numba, dist_numpy
# import dist_cython
# from slop_tracking_solver import slop_tracking
import matplotlib
# matplotlib.use('Agg')
from matplotlib import pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from matplotlib import cm
import multiprocessing as ms
import concurrent.futures
import scipy.interpolate as sfit
import copy
import cv2
# from noise_image import add_noise


def load_image(file_path):
    if os.path.exists(file_path):
        img = np.array(Image.open(file_path))
    else:
        prColor('Error: wrong data path. No data is loaded.', 'red')
        sys.exit()
    return np.array(img)


def image_preprocess(image, have_dark, dark_img, have_flat, flat_img):
    '''
        do the flat or dark correction for the images
        img:            image to be corrected
        have_dark:      if there is dark
        dark_img:           dark image
        have_flat:      if there is flat
        flat_img:           flat image        
    '''
    if (have_flat != 0 and have_dark != 0):
        numerator = (flat_img - dark_img).clip(0.00000001)
        # numerator = numerator / np.amax(numerator)
        image = ((image - dark_img) / numerator) * np.amax(image)
    elif (have_dark != 0):
        image = (image - dark_img).clip(0.00000001)
    elif (have_flat != 0):
        flat_img[flat_img == 0] = 0.00000001
        # flat_img = flat_img / np.amax(flat_img)
        image = (image / flat_img) * np.amax(image)

    return image


def slope_tracking(img, ref, n_window=15):
    '''
        use opencv optical flow function to calculate the moving of the pixels.
        input:
            img:            the sample image
            ref:            the reference image
        output:
            displace:       the displacement of the pixels in the images
                            [dips_H, disp_V]
    '''
    # the pyramid scale, make the undersampling image
    pyramid_scal = 0.5
    # the pyramid levels
    levels = 2
    # window size of the displacement calculation
    winsize = n_window
    # iteration for the calculation
    n_iter = 10
    # neighborhood pixel size to calculate the polynomial expansion, which makes the results smooth but blurred
    n_poly = 3
    # standard deviation of the Gaussian that is used to smooth derivatives used as a basis for
    # the polynomial expansion; for poly_n=5, you can set poly_sigma=1.1, for poly_n=7, a good value would be poly_sigma=1.5.
    sigma_poly = 1.2
    '''
        operation flags that can be a combination of the following:

        OPTFLOW_USE_INITIAL_FLOW uses the input flow as an initial flow approximation.
        OPTFLOW_FARNEBACK_GAUSSIAN uses the Gaussian \texttt{winsize}\times\texttt{winsize} filter instead of 
        a box filter of the same size for optical flow estimation; usually, this option gives z more accurate 
        flow than with a box filter, at the cost of lower speed; normally, winsize for a Gaussian window 
        should be set to a larger value to achieve the same level of robustness.
    '''
    flags = 1

    flow = cv2.calcOpticalFlowFarneback(ref, img, None, pyramid_scal, levels,
                                        winsize, n_iter, n_poly, sigma_poly,
                                        flags)

    displace = np.array([flow[..., 1], flow[..., 0]])

    return displace


class WXST:
    def __init__(self,
                img,
                ref,
                M_image=512,
                N_s=5,
                cal_half_window=20,
                N_s_extend=4,
                n_cores=4,
                n_group=4,
                energy=14e3,
                p_x=0.65e-6,
                z=500e-3,
                wavelet_level_cut=2,
                pyramid_level=2,
                n_iter=1, use_estimate=False, use_wavelet=True):
        self.img_data = img
        self.ref_data = ref
        # roi of the images
        self.M_image = M_image
        # template window, the N_s nearby pixels used to represent the local pixel, 2*N_s+1
        self.N_s = N_s
        # the number of the area to calculate for each pixel, 2*cal_half_window X 2*cal_half_window
        self.cal_half_window = cal_half_window
        # the calculation window for high order pyramid
        self.N_s_extend = N_s_extend

        # process number for parallel
        self.n_cores = n_cores
        # number to reduce the each memory use
        self.n_group = n_group

        # energy, 10kev
        self.energy = energy
        self.wavelength = sc.value(
            'inverse meter-electron volt relationship') / energy
        # pixel size [m]
        self.p_x = p_x
        # distance [m]
        self.z = z
        self.wavelet_level_cut = wavelet_level_cut
        # pyramid level to wrap the images
        self.pyramid_level = pyramid_level
        # iterations for the calculation
        self.n_iter = n_iter
        # if use the estimated displace as initial guess
        self.use_estimate = use_estimate
        # if use wavelet transform or not
        self.use_wavelet = use_wavelet

        if self.use_estimate:
            # get initial estimation from the cv2 flow tracking
            displace_estimate = slope_tracking(self.ref_data, self.img_data, n_window=self.cal_half_window)
            self.displace_estimate = [displace_estimate[0], displace_estimate[1]]
        else:
            m, n= self.img_data.shape
            self.displace_estimate = [np.zeros((m, n)), np.zeros((m, n))]
        

    def template_stack(self, img):
        '''
            stack the nearby pixels in 2*N_s+1
        '''
        img_stack = []
        axis_Nw = np.arange(-self.N_s, self.N_s + 1)
        for x in axis_Nw:
            for y in axis_Nw:
                img_stack.append(np.roll(np.roll(img, x, axis=0), y, axis=1))

        return np.array(img_stack)

    def pyramid_data(self):
        # get the pyramid data
        # method 1, pyramid then stack the image, which means the template window size is increasing for different pyramid level
        # get the pyramid wrapping images
        ref_pyramid = []
        img_pyramid = []
        prColor('obtain pyramid image and stack the window with pyramid level: {}'.format(self.pyramid_level), 'green')
        ref_pyramid.append(self.ref_data)
        img_pyramid.append(self.img_data)

        for kk in range(self.pyramid_level):
            ref_pyramid.append(
                pywt.dwtn(ref_pyramid[kk], 'db3', mode='zero',
                        axes=(-2, -1))['aa'])
            img_pyramid.append(
                pywt.dwtn(img_pyramid[kk], 'db3', mode='zero',
                        axes=(-2, -1))['aa'])

        normlize_std = lambda img: (
            (img - np.ndarray.mean(img, axis=0)) / np.ndarray.std(img, axis=0))

        ref_pyramid = [normlize_std(self.template_stack(img_data)) for img_data in ref_pyramid]
        img_pyramid = [normlize_std(self.template_stack(img_data)) for img_data in img_pyramid]
        
        return ref_pyramid, img_pyramid

    def resampling_spline(self, img, s):
        # img: original 
        # s: size of the sampling, (row, col)
        m, n = img.shape
        x_axis = np.arange(n)
        y_axis = np.arange(m)
        fit = sfit.RectBivariateSpline(y_axis, x_axis, img)

        x_new = np.linspace(0, n-1, s[1])
        y_new = np.linspace(0, m-1, s[0])

        return fit(y_new, x_new)

    def wavelet_data(self):
        # process the data to get the wavelet transform
        ref_pyramid, img_pyramid = self.pyramid_data()
        if self.use_wavelet:
            prColor('obtain wavelet data...', 'green')
            wavelet_method = 'db2'
            # wavelet_method = 'bior1.3'
            # wavelet wrapping level. 2 is half, 3 is 1/3 of the size
            max_wavelet_level = pywt.dwt_max_level(ref_pyramid[0].shape[0],
                                                wavelet_method)
            prColor('max wavelet level: {}'.format(max_wavelet_level), 'green')
            self.wavelet_level = max_wavelet_level
            coefs_level = self.wavelet_level + 1 - self.wavelet_level_cut

            if ref_pyramid[0].shape[0] > 150:
                self.wavelet_add_list = [0, 0, 0, 0, 0, 0]
            elif ref_pyramid[0].shape[0] > 50:
                self.wavelet_add_list = [0, 0, 1, 2, 2, 2]
            else:
                self.wavelet_add_list = [2, 2, 2, 2, 2, 2]
            
            # wavelet transform and cut for the pyramid images
            start_time = time.time()
            for p_level in range(len(img_pyramid)):
                if p_level > len(self.wavelet_add_list):
                    wavelevel_add = 2
                else:
                    wavelevel_add = self.wavelet_add_list[p_level]

                img_wa, level_name = Wavelet_transform(img_pyramid[p_level],
                                                        wavelet_method,
                                                        w_level=self.wavelet_level,
                                                        return_level=coefs_level+wavelevel_add)
                img_pyramid[p_level] = img_wa

                ref_wa, level_name = Wavelet_transform(ref_pyramid[p_level],
                                                        wavelet_method,
                                                        w_level=self.wavelet_level,
                                                        return_level=coefs_level+wavelevel_add)
                ref_pyramid[p_level] = ref_wa

                prColor(
                    'pyramid level: {}\nvector length: {}\nUse wavelet coef: {}'.format(p_level,
                        ref_wa.shape[2], level_name), 'green')

            end_time = time.time()
            print('wavelet time: {}'.format(end_time - start_time))
        else:
            img_pyramid = [np.moveaxis(img_data, 0, -1) for img_data in img_pyramid]
            ref_pyramid = [np.moveaxis(img_data, 0, -1) for img_data in ref_pyramid]
            self.wavelet_level = None
            self.wavelet_add_list = None
            self.wavelet_level_cut = None

        return ref_pyramid, img_pyramid
    
    def displace_wavelet(self, y_list, img_wa_stack, ref_wa_stack, displace_pyramid,
                     cal_half_window, n_pad):
        '''
            calculate the coefficient of each pixel
        '''
        dim = img_wa_stack.shape
        disp_x = np.zeros((dim[0], dim[1]))
        disp_y = np.zeros((dim[0], dim[1]))

        # the axis for the peak position finding
        window_size = 2 * cal_half_window + 1
        y_axis = np.arange(window_size) - cal_half_window
        x_axis = np.arange(window_size) - cal_half_window
        XX, YY = np.meshgrid(x_axis, y_axis)

        for yy in range(dim[0]):
            for xx in range(dim[1]):
                img_wa_line = img_wa_stack[yy, xx, :]
                ref_wa_data = ref_wa_stack[n_pad+yy+int(displace_pyramid[0][yy, xx]):n_pad+yy+int(displace_pyramid[0][yy, xx]) + window_size,
                                        n_pad+xx+int(displace_pyramid[1][yy, xx]):n_pad+xx+int(displace_pyramid[1][yy, xx]) + window_size, :]

                # get the correlation matrix
                # use different calculation method, maybe faster
                # Corr_img = np.tensordot(-img_wa_line, ref_wa_data, axes = ([0],[0]))
                '''
                    euclidean distance
                '''

                # Corr_img = np.negative(np.sum((img_wa_line-ref_wa_data)**2, axis=2))
                Corr_img = dist_numba(img_wa_line, ref_wa_data)
                # Corr_img = dist_cython.dist_cal(img_wa_line, ref_wa_data)
                # Corr_img = dist_numpy(img_wa_line, ref_wa_data)

                # Corr_img = (Corr_img - np.amin(Corr_img)) / (np.amax(Corr_img) - np.amin(Corr_img))
                '''
                    use gradient to find the peak
                '''
                disp_y[yy, xx], disp_x[yy, xx], SN_ratio, max_corr = find_disp(
                    Corr_img, XX, YY, sub_resolution=True)
                # disp_y[yy, xx], disp_x[yy, xx] = find_disp_2nd(Corr_img, XX, YY)

        disp_add_y = displace_pyramid[0] + disp_y
        disp_add_x = displace_pyramid[1] + disp_x
        return disp_add_y, disp_add_x, y_list

    def solver(self):

        ref_pyramid, img_pyramid = self.wavelet_data()
        transmission = self.img_data/self.ref_data
        for attr in ('img_data','ref_data'):
            self.__dict__.pop(attr,None)

        cores = ms.cpu_count()
        prColor('Computer available cores: {}'.format(cores), 'green')

        if cores > self.n_cores:
            cores = self.n_cores
        else:
            cores = ms.cpu_count()
        prColor('Use {} cores'.format(cores), 'light_purple')
        prColor('Process group number: {}'.format(self.n_group), 'light_purple')

        if cores * self.n_group > self.M_image:
            n_tasks = 4
        else:
            n_tasks = cores * self.n_group


        start_time = time.time()
        # use pyramid wrapping
        max_pyramid_searching_window = int(np.ceil(self.cal_half_window / 2**self.pyramid_level))
        searching_window_pyramid_list = [self.N_s_extend]*self.pyramid_level+[int(max_pyramid_searching_window)]

        displace = self.displace_estimate
        
        for k_iter in range(self.n_iter):
            # iteration to approximating the results
            displace = [img/2**self.pyramid_level for img in displace]
            
            m, n, c = img_pyramid[-1].shape
            displace[0] = self.resampling_spline(displace[0], (m,n))
            displace[1] = self.resampling_spline(displace[1], (m,n))

            prColor('down sampling the dispalce to size: {}'.format(displace[0].shape), 'green')

            displace = [np.fmax(np.fmin(displace[0], self.cal_half_window/2**self.pyramid_level), -self.cal_half_window/2**self.pyramid_level),
                         np.fmax(np.fmin(displace[1], self.cal_half_window/2**self.pyramid_level), -self.cal_half_window/2**self.pyramid_level)]

            for p_level in range(self.pyramid_level, -1, -1):
                # first pyramid, searching the window. Then search nearby
                if p_level == self.pyramid_level:
                    pyramid_seaching_window = searching_window_pyramid_list[p_level]
                    m, n, c = img_pyramid[p_level].shape
                    displace_pyramid = [np.round(img) for img in displace]
                    
                    # n_pad = int(np.ceil(cal_half_window / 2**p_level) * 2**(pyramid_level-p_level))
                    n_pad = int(np.ceil(self.cal_half_window / 2**p_level))

                else:
                    pyramid_seaching_window = searching_window_pyramid_list[p_level]
                    # extend displace_pyramid with upsampling of 2 and also displace value is 2 times larger
                    m, n, c = img_pyramid[p_level].shape
                    displace_pyramid = [np.round(self.resampling_spline(img*2, (m, n))) for img in displace]

                    displace_pyramid = [np.fmax(np.fmin(displace_pyramid[0], self.cal_half_window/2**p_level), -self.cal_half_window/2**p_level),
                                         np.fmax(np.fmin(displace_pyramid[1], self.cal_half_window/2**p_level), -self.cal_half_window/2**p_level)]

                    # n_pad = int(np.ceil(cal_half_window / 2**p_level) * 2**(pyramid_level-p_level))
                    n_pad = int(np.ceil(self.cal_half_window / 2**p_level)) 
                # print(displace_pyramid[0].shape, np.amax(displace_pyramid[0]), np.amin(displace_pyramid[0]))
                prColor(
                    'pyramid level: {}\nImage size: {}\nsearching window:{}'.format(
                        p_level, ref_pyramid[p_level].shape, pyramid_seaching_window), 'cyan')
                # split the y axis into small groups, all splitted in vertical direction
                y_axis = np.arange(ref_pyramid[p_level].shape[0])
                chunks_idx_y = np.array_split(y_axis, n_tasks)

                dim = img_pyramid[p_level].shape

                ref_wa_pad = np.pad(ref_pyramid[p_level],
                                    ((n_pad+pyramid_seaching_window, n_pad+pyramid_seaching_window),
                                    (n_pad+pyramid_seaching_window, n_pad+pyramid_seaching_window), (0, 0)),
                                    'constant',
                                    constant_values=(0, 0))
                
                # use CPU parallel to calculate
                result_list = []
                '''
                    calculate the pixel displacement for the pyramid images
                '''
                with concurrent.futures.ProcessPoolExecutor(max_workers=cores) as executor:

                    futures = []
                    for y_list in chunks_idx_y:
                        # get the stack data
                        img_wa_stack = img_pyramid[p_level][y_list, :, :]
                        ref_wa_stack = ref_wa_pad[y_list[0]:y_list[-1] +
                                                2 * (n_pad+pyramid_seaching_window) + 1, :, :]

                        # start the jobs
                        futures.append(
                            executor.submit(self.displace_wavelet, y_list, img_wa_stack, ref_wa_stack, (displace_pyramid[0][y_list,:], displace_pyramid[1][y_list,:]), pyramid_seaching_window, n_pad))

                    for future in concurrent.futures.as_completed(futures):

                        try:
                            result_list.append(future.result())
                            # display the status of the program
                            Total_iter = cores * self.n_group
                            Current_iter = len(result_list)
                            percent_iter = Current_iter / Total_iter * 100
                            str_bar = '>' * (int(np.ceil(percent_iter / 2))) + ' ' * (int(
                                (100 - percent_iter) // 2))
                            prColor(
                                '\r' + str_bar + 'processing: [%3.1f%%] ' % (percent_iter),
                                'purple')

                        except:
                            prColor('Error in the parallel calculation', 'red')

                disp_y_list = [item[0] for item in result_list]
                disp_x_list = [item[1] for item in result_list]
                y_list = [item[2] for item in result_list]

                displace_y = np.zeros((dim[0], dim[1]))
                displace_x = np.zeros((dim[0], dim[1]))
                # x_axis = np.zeros(XX.shape)
                # y_axis = np.zeros(XX.shape)

                for y, disp_x, disp_y in zip(y_list, disp_x_list, disp_y_list):
                    displace_x[y, :] = disp_x
                    displace_y[y, :] = disp_y

                displace = [np.fmax(np.fmin(displace_y, self.cal_half_window/2**p_level), -self.cal_half_window/2**p_level),
                             np.fmax(np.fmin(displace_x, self.cal_half_window/2**p_level), -self.cal_half_window/2**p_level)]
                prColor('displace map wrapping: {}'.format(displace[0].shape), 'green')
                print('max of displace: {}, min of displace: {}'.format(
                np.amax(displace[0]), np.amin(displace[1])))

        end_time = time.time()
        prColor('\r' + 'Processing time: {:0.3f} s'.format(end_time - start_time),
                'light_purple')

        # remove the padding boundary of the displacement
        displace[0] = -displace[0][self.cal_half_window:-self.cal_half_window,
                                self.cal_half_window:-self.cal_half_window]
        displace[1] = -displace[1][self.cal_half_window:-self.cal_half_window,
                                self.cal_half_window:-self.cal_half_window]
        # '''
        #     do the filter to smooth the image
        # '''
        # displace[0] = filter_erosion(
        #     displace[0], abs(2.0 * np.mean(np.diff(displace[0], 1, 0))))
        # displace[1] = filter_erosion(
        #     displace[1], abs(2.0 * np.mean(np.diff(displace[1], 1, 0))))
        
        # print(displace[0].shape)
        DPC_y = (displace[0] - np.mean(displace[0])) * p_x / z
        DPC_x = (displace[1] - np.mean(displace[1])) * p_x / z

        phase = -frankotchellappa(DPC_x, DPC_y) * self.p_x * 2 * np.pi / self.wavelength
        self.time_cost = end_time - start_time

        return displace, [DPC_y, DPC_x], phase, transmission

    def run(self, result_path=None):
        self.displace, self.DPC, self.phase, self.transmission = self.solver()
        
        if result_path is not None:
            '''
            save the calculation results
            '''
            if not os.path.exists(result_path):
                os.makedirs(result_path)

            self.result_filename = 'WXST' + str(self.M_image) + '_px_' + str(
                self.wavelet_level_cut) + 'wavelet_Cutlevel_' + str(
                    self.pyramid_level) + 'pyramid_level'

            kk = 1
            while os.path.exists(os.path.join(result_path, self.result_filename+'.hdf5')) or os.path.exists(os.path.join(result_path, self.result_filename+'.json')):
                self.result_filename = 'WXST' + str(self.M_image) + '_px_' + str(
                self.wavelet_level_cut) + 'wavelet_Cutlevel_' + str(
                    self.pyramid_level) + 'pyramid_level'+'_{}'.format(kk)
                kk += 1
            write_h5(
                result_path, self.result_filename, {
                    'displace_x': self.displace[1],
                    'displace_y': self.displace[0],
                    'DPC_x': self.DPC[1],
                    'DPC_y': self.DPC[0],
                    'phase': self.phase,
                    'transmission_image': self.transmission
                })

            parameter_dict = {
                'M_image': self.M_image,
                'template_window': self.N_s,
                'N_s extend': self.N_s_extend,
                'half_window': self.cal_half_window,
                'energy': self.energy,
                'wavelength': self.wavelength,
                'pixel_size': self.p_x,
                'z_distance': self.z,
                'cpu_cores': self.n_cores,
                'n_group': self.n_group,
                'wavelet_level': self.wavelet_level,
                'pyramid_level': self.pyramid_level,
                'n_iter': self.n_iter,
                'time_cost': self.time_cost,
                'use_wavelet': self.use_wavelet,
                'wavelet_level_cut': self.wavelet_level_cut,
                'wavelet_add': self.wavelet_add_list
            }

            write_json(result_path, self.result_filename, parameter_dict)
    

if __name__ == "__main__":
    if len(sys.argv) == 1:

        # Folder_ref = 'H:/data/Jan2020_speckle/20200202/single_shot_18XCRL200um/d310mm/linear_sandpaper/refs/'
        # Folder_img = 'H:/data/Jan2020_speckle/20200202/single_shot_18XCRL200um/d310mm/linear_sandpaper/sample_in/'
        # Folder_result = 'H:/data/Jan2020_speckle/20200202/single_shot_18XCRL200um/d310mm/linear_sandpaper/wavelet_pyramid/'

        Folder_path = 'D:/data/Jan2020_speckle/20200202/single_shot/d500mm/sandpaper_ExpTime5s'
        File_ref = os.path.join(Folder_path, 'ref_001.tif')
        File_img = os.path.join(Folder_path, 'sample_001.tif')
        # File_flat = os.path.join(Folder_path, 'flat_5000ms_004.tif')

        Folder_result = os.path.join(Folder_path, 'WXST_test')
        # [image_size, template_window, cal_half_window, n_group, n_cores, energy, pixel_size, distance, use_wavelet, wavelet_ct, pyramid level, n_iteration]
        parameter_wavelet = [1500, 7, 20, 4, 4, 14e3, 0.65e-6, 310e-3, 0, 2, 2, 1]

    elif len(sys.argv) == 4:
        File_img = sys.argv[1]
        File_ref = sys.argv[2]
        Folder_result = sys.argv[3]
        # [image_size, template_window, cal_half_window, n_group, n_cores, energy, pixel_size, distance, wavelet_ct, pyramid level, n_iteration]
        parameter_wavelet = [1500, 5, 20, 4, 4, 14e3, 0.65e-6, 500e-3, 1, 2, 2, 1]
    elif len(sys.argv) == 16:
        File_img = sys.argv[1]
        File_ref = sys.argv[2]
        Folder_result = sys.argv[3]
        parameter_wavelet = sys.argv[4:]
    else:
        prColor('Wrong parameters! should be: sample, ref, result', 'red')

    prColor('folder: {}'.format(Folder_result), 'green')
    # roi of the images
    M_image = int(parameter_wavelet[0])
    # template window, the N_s nearby pixels used to represent the local pixel, 2*N_s+1
    N_s = int(parameter_wavelet[1])
    # the number of the area to calculate for each pixel, 2*cal_half_window X 2*cal_half_window
    cal_half_window = int(parameter_wavelet[2])
    # the calculation window for high order pyramid
    N_s_extend = 4

    # process number for parallel
    n_cores = int(parameter_wavelet[3])
    # number to reduce the each memory use
    n_group = int(parameter_wavelet[4])

    # energy, 10kev
    energy = float(parameter_wavelet[5])
    wavelength = sc.value('inverse meter-electron volt relationship') / energy
    p_x = float(parameter_wavelet[6])
    z = float(parameter_wavelet[7])
    use_wavelet = int(parameter_wavelet[8])
    wavelet_level_cut = int(parameter_wavelet[9])
    # pyramid level to wrap the images
    pyramid_level = int(parameter_wavelet[10])
    n_iter = int(parameter_wavelet[11])

    ref_data = load_image(File_ref)
    img_data = load_image(File_img)

    # radio_flat = load_images(Folder_radio, 'flat*.tif')
    # radio_dark = load_images(Folder_radio, 'dark*.tif')

    # ref_data = image_preprocess(ref_data, have_dark=1, dark_img=radio_dark, have_flat=1, flat_img=radio_flat)
    # img_data = image_preprocess(img_data, have_dark=1, dark_img=radio_dark, have_flat=1, flat_img=radio_flat)

    # take out the roi
    ref_data = image_roi(ref_data, M_image)
    img_data = image_roi(img_data, M_image)

    WXST_solver = WXST(img_data,
                     ref_data,
                     M_image=M_image,
                     N_s=N_s,
                     cal_half_window=cal_half_window,
                     N_s_extend=N_s_extend,
                     n_cores=n_cores,
                     n_group=n_group,
                     energy=energy,
                     p_x=p_x,
                     z=z,
                     wavelet_level_cut=wavelet_level_cut,
                     pyramid_level=pyramid_level,
                     n_iter=n_iter, use_wavelet=use_wavelet, use_estimate=False)

    # # get initial estimation from the cv2 flow tracking
    # displace_estimate = slope_tracking(ref_data,
    #                                    img_data,
    #                                    N_window=cal_half_window)

    if not os.path.exists(Folder_result):
        os.makedirs(Folder_result)
    sample_transmission = img_data / ref_data
    plt.imsave(os.path.join(Folder_result, 'transmission.png'),
               sample_transmission)

    WXST_solver.run(result_path=Folder_result)

    displace = WXST_solver.displace
    DPC_x = WXST_solver.DPC[1]
    DPC_y = WXST_solver.DPC[0]
    phase = WXST_solver.phase
    result_filename = WXST_solver.result_filename

    # use slop_tracking to find the wavefront for one image
    # phase, displace = slop_tracking(img_data[0], ref_data[0], p_x, z)

    plt.imsave(os.path.join(Folder_result, 'displace_x.png'), displace[1])
    plt.imsave(os.path.join(Folder_result, 'displace_y.png'), displace[0])
    plt.imsave(os.path.join(Folder_result, 'dpc_x.png'), DPC_x)
    plt.imsave(os.path.join(Folder_result, 'dpc_y.png'), DPC_y)
    plt.imsave(os.path.join(Folder_result, 'phase.png'), phase)

    plt.figure()
    plt.imshow(displace[0])
    cbar = plt.colorbar()
    cbar.set_label('[pixels]', rotation=90)
    plt.savefig(os.path.join(Folder_result, 'displace_y_colorbar.png'))
    plt.figure()
    plt.imshow(displace[1])
    cbar = plt.colorbar()
    cbar.set_label('[pixels]', rotation=90)
    plt.savefig(os.path.join(Folder_result, 'displace_x_colorbar.png'))
    plt.figure()
    plt.imshow(phase)
    cbar = plt.colorbar()
    cbar.set_label('[rad]', rotation=90)
    plt.savefig(os.path.join(Folder_result, 'phase_colorbar.png'))
    # plt.show()
    fig = plt.figure()
    ax1 = fig.add_subplot(111, projection='3d')
    XX, YY = np.meshgrid(
        np.arange(phase.shape[1]) * p_x * 1e6,
        np.arange(phase.shape[0]) * p_x * 1e6)
    ax1.plot_surface(XX, YY, phase, cmap=cm.get_cmap('hot'))
    ax1.set_xlabel('x [$\mu m$]')
    ax1.set_ylabel('y [$\mu m$]')
    ax1.set_zlabel('phase [rad]')
    plt.savefig(os.path.join(Folder_result, 'Phase_3d.png'))

    plt.close()

    # d_speckle_sample = 0e-3
    # R_dia = '300e-6 400e-6'
    # hdf5_file = os.path.join(Folder_result, result_filename + '.hdf5')
    # json_file = os.path.join(Folder_result, result_filename + '.json')

    # os.system("python data_PostProcess.py " + hdf5_file + ' ' + json_file +
    #           " {}".format(d_speckle_sample) + " {}".format(R_dia))