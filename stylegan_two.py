from PIL import Image
from math import floor, log2
import numpy as np
import time
from random import random
import pathlib

from tensorflow.keras.layers import *
from tensorflow.keras.models import *
from tensorflow.keras.optimizers import *
from tensorflow.keras.initializers import *

from datagen import DataGenerator, printProgressBar
from conv_mod import *


# Loss functions
def gradient_penalty(samples, output, weight):
    gradients = K.gradients(output, samples)[0]
    gradients_sqr = K.square(gradients)
    _gradient_penalty = K.sum(gradients_sqr, axis=np.arange(1, len(gradients_sqr.shape)))

    # (weight / 2) * ||grad||^2
    # Penalize the gradient norm
    return K.mean(_gradient_penalty) * weight


def hinge_d(y_true, y_pred):
    return K.mean(K.relu(1.0 + (y_true * y_pred)))


def w_loss(y_true, y_pred):
    return K.mean(y_true * y_pred)


# Lambdas
def crop_to_fit(x):
    height = x[1].shape[1]
    width = x[1].shape[2]

    return x[0][:, :height, :width, :]


def upsample(x):
    return K.resize_images(x, 2, 2, "channels_last", interpolation='bilinear')

#
# def from_rgb(inp, conc=None):
#     fil = int(im_size * 4 / inp.shape[2])
#     z = AveragePooling2D()(inp)
#     x = Conv2D(fil, 1, kernel_initializer='he_uniform')(z)
#     if conc is not None:
#         x = concatenate([x, conc])
#     return x, z


class GAN(object):

    def __init__(self, steps=1, lr=0.0001, decay=0.00001, latent_size=512, img_size=128, cha=12):

        self.latent_size = latent_size
        self.img_size = img_size

        self.n_layers = int(log2(self.img_size) - 1)

        self.cha = cha

        # Models
        self.D = None
        self.S = None
        self.G = None

        self.GE = None
        self.SE = None

        self.DM = None
        self.AM = None

        # Config
        self.LR = lr
        self.steps = steps
        self.beta = 0.999

        # Init Models
        self.discriminator()
        self.generator()

        self.GMO = Adam(lr=self.LR, beta_1=0, beta_2=0.999)
        self.DMO = Adam(lr=self.LR, beta_1=0, beta_2=0.999)

        self.GE = clone_model(self.G)
        self.GE.set_weights(self.G.get_weights())

        self.SE = clone_model(self.S)
        self.SE.set_weights(self.S.get_weights())

    def make_uts(self, s1=4, s2=64):
        ss = int(s2 / s1)

        def upsample_to_size(x, y=ss):
            x = K.resize_images(x, y, y, "channels_last", interpolation='bilinear')
            return x

        return upsample_to_size

    def to_rgb(self, inp, style):
        size = inp.shape[2]
        x = Conv2DMod(3, 1, kernel_initializer=VarianceScaling(200 / size), demod=False)([inp, style])
        return Lambda(self.make_uts(size, self.img_size), output_shape=[None, self.img_size, self.img_size, None])(x)

    # Blocks
    def g_block(self, inp, istyle, inoise, fil, u=True):
        if u:
            # Custom upsampling because of clone_model issue
            out = UpSampling2D(interpolation='bilinear')(inp)
        else:
            out = Activation('linear')(inp)

        rgb_style = Dense(fil, kernel_initializer=VarianceScaling(200 / out.shape[2]))(istyle)
        style = Dense(inp.shape[-1], kernel_initializer='he_uniform')(istyle)
        delta = Lambda(crop_to_fit)([inoise, out])
        d = Dense(fil, kernel_initializer='zeros')(delta)

        out = Conv2DMod(filters=fil, kernel_size=3, padding='same', kernel_initializer='he_uniform')([out, style])
        out = add([out, d])
        out = LeakyReLU(0.2)(out)

        style = Dense(fil, kernel_initializer='he_uniform')(istyle)
        d = Dense(fil, kernel_initializer='zeros')(delta)

        out = Conv2DMod(filters=fil, kernel_size=3, padding='same', kernel_initializer='he_uniform')([out, style])
        out = add([out, d])
        out = LeakyReLU(0.2)(out)

        return out, self.to_rgb(out, rgb_style)

    def d_block(self, inp, fil, p=True):
        res = Conv2D(fil, 1, kernel_initializer='he_uniform')(inp)

        out = Conv2D(filters=fil, kernel_size=3, padding='same', kernel_initializer='he_uniform')(inp)
        out = LeakyReLU(0.2)(out)
        out = Conv2D(filters=fil, kernel_size=3, padding='same', kernel_initializer='he_uniform')(out)
        out = LeakyReLU(0.2)(out)

        out = add([res, out])

        if p:
            out = AveragePooling2D()(out)

        return out

    def discriminator(self):

        if self.D:
            return self.D

        inp = Input(shape=[self.img_size, self.img_size, 3])

        x = self.d_block(inp, 1 * self.cha)  # 128

        x = self.d_block(x, 2 * self.cha)  # 64

        x = self.d_block(x, 4 * self.cha)  # 32

        x = self.d_block(x, 8 * self.cha)  # 16

        x = self.d_block(x, 16 * self.cha, p=False)  # 8

        # x = d_block(x, 16 * self.cha)  #4

        # x = d_block(x, 32 * self.cha, p = False)  #4

        x = Flatten()(x)

        x = Dense(1, kernel_initializer='he_uniform')(x)

        self.D = Model(inputs=inp, outputs=x)

        return self.D

    def generator(self):

        if self.G:
            return self.G

        # === Style Mapping ===

        self.S = Sequential()

        self.S.add(Dense(512, input_shape=[self.latent_size]))
        self.S.add(LeakyReLU(0.2))
        self.S.add(Dense(512))
        self.S.add(LeakyReLU(0.2))
        self.S.add(Dense(512))
        self.S.add(LeakyReLU(0.2))
        self.S.add(Dense(512))
        self.S.add(LeakyReLU(0.2))

        # === Generator ===

        # Inputs
        inp_style = []

        for i in range(self.n_layers):
            inp_style.append(Input([512]))

        inp_noise = Input([self.img_size, self.img_size, 1])

        # Latent
        x = Lambda(lambda x: x[:, :1] * 0 + 1)(inp_style[0])

        outs = []

        # Actual Model
        x = Dense(4 * 4 * 4 * self.cha, activation='relu', kernel_initializer='random_normal')(x)
        x = Reshape([4, 4, 4 * self.cha])(x)

        x, r = self.g_block(x, inp_style[0], inp_noise, 32 * self.cha, u=False)  # 4
        outs.append(r)

        # x, r = g_block(x, inp_style[1], inp_noise, 16 * self.cha)  #8
        # outs.append(r)

        x, r = self.g_block(x, inp_style[1], inp_noise, 8 * self.cha)  # 16
        outs.append(r)

        # x, r = g_block(x, inp_style[3], inp_noise, 6 * self.cha)  #32
        # outs.append(r)

        x, r = self.g_block(x, inp_style[2], inp_noise, 4 * self.cha)  # 64
        outs.append(r)

        x, r = self.g_block(x, inp_style[3], inp_noise, 2 * self.cha)  # 128
        outs.append(r)

        x, r = self.g_block(x, inp_style[4], inp_noise, 1 * self.cha)  # 256
        outs.append(r)

        x = add(outs)

        x = Lambda(lambda y: y / 2 + 0.5)(
            x)  # Use values centered around 0, but normalize to [0, 1], providing better initialization

        self.G = Model(inputs=inp_style + [inp_noise], outputs=x)

        return self.G, self.S

    def gen_model(self):

        # Generator Model for Evaluation

        inp_style = []
        style = []

        for i in range(self.n_layers):
            inp_style.append(Input([self.latent_size]))
            style.append(self.S(inp_style[-1]))

        inp_noise = Input([self.img_size, self.img_size, 1])

        gf = self.G(style + [inp_noise])

        self.GM = Model(inputs=inp_style + [inp_noise], outputs=gf)

        return self.GM

    def gen_model_a(self):

        # Parameter Averaged Generator Model

        inp_style = []
        style = []

        for i in range(self.n_layers):
            inp_style.append(Input([self.latent_size]))
            style.append(self.SE(inp_style[-1]))

        inp_noise = Input([self.img_size, self.img_size, 1])

        gf = self.GE(style + [inp_noise])

        self.GMA = Model(inputs=inp_style + [inp_noise], outputs=gf)

        return self.GMA

    def ema(self):

        # Parameter Averaging

        for i in range(len(self.G.layers)):
            up_weight = self.G.layers[i].get_weights()
            old_weight = self.GE.layers[i].get_weights()
            new_weight = []
            for j in range(len(up_weight)):
                new_weight.append(old_weight[j] * self.beta + (1 - self.beta) * up_weight[j])
            self.GE.layers[i].set_weights(new_weight)

        for i in range(len(self.S.layers)):
            up_weight = self.S.layers[i].get_weights()
            old_weight = self.SE.layers[i].get_weights()
            new_weight = []
            for j in range(len(up_weight)):
                new_weight.append(old_weight[j] * self.beta + (1 - self.beta) * up_weight[j])
            self.SE.layers[i].set_weights(new_weight)

    def ma_init(self):
        # Reset Parameter Averaging
        self.GE.set_weights(self.G.get_weights())
        self.SE.set_weights(self.S.get_weights())


class StyleGAN(object):

    def __init__(self, directory, save_directory, steps=1, lr=0.0001, decay=0.00001, silent=True, latent_size=512, img_size=128, batch_size=6):

        self.directory = directory
        self.save_directory = pathlib.Path(save_directory)

        self.results_path = self.save_directory / 'results'
        self.results_path.mkdir(parents=True, exist_ok=True)

        self.models_path = self.save_directory / 'models'
        self.model_path.mkdir(parents=True, exist_ok=True)

        self.latent_size = latent_size
        self.img_size = img_size

        self.n_layers = int(log2(self.img_size) - 1)

        self.mixed_prob = 0.9

        self.batch_size = batch_size

        # Init GAN and Eval Models
        self.GAN = GAN(steps=steps, lr=lr, decay=decay, latent_size=512, img_size=128)
        self.GAN.gen_model()
        self.GAN.gen_model_a()

        self.GAN.G.summary()

        # Data generator (my own code, not from TF 2.0)
        self.im = None

        # Set up variables
        self.lastblip = time.clock()

        self.silent = silent

        self.ones = np.ones((self.batch_size, 1), dtype=np.float32)
        self.zeros = np.zeros((self.batch_size, 1), dtype=np.float32)
        self.nones = -self.ones

        self.evaluate("nit")

        self.pl_mean = 0
        self.av = np.zeros([44])

    def n_image(self, n):
        return np.random.uniform(0.0, 1.0, size=[n, self.img_size, self.img_size, 1]).astype('float32')

    def noise(self, n):
        return np.random.normal(0.0, 1.0, size=[n, self.latent_size]).astype('float32')

    def noise_list(self, n):
        return [self.noise(n)] * self.n_layers

    def mixed_list(self, n):
        tt = int(random() * self.n_layers)
        p1 = [self.noise(n)] * tt
        p2 = [self.noise(n)] * (self.n_layers - tt)
        return p1 + [] + p2

    def train(self):
        self.im = DataGenerator(self.directory, self.img_size, flip=True)

        # Train Alternating
        if random() < self.mixed_prob:
            style = self.mixed_list(self.batch_size)
        else:
            style = self.noise_list(self.batch_size)

        # Apply penalties every 16 steps
        apply_gradient_penalty = self.GAN.steps % 2 == 0 or self.GAN.steps < 10000
        apply_path_penalty = self.GAN.steps % 16 == 0

        a, b, c, d = self.train_step(
            self.im.get_batch(self.batch_size).astype('float32'),
            style,
            self.n_image(self.batch_size),
            apply_gradient_penalty,
            apply_path_penalty
        )

        # Adjust path length penalty mean
        # d = pl_mean when no penalty is applied
        if self.pl_mean == 0:
            self.pl_mean = np.mean(d)
        self.pl_mean = 0.99 * self.pl_mean + 0.01 * np.mean(d)

        if self.GAN.steps % 10 == 0 and self.GAN.steps > 20000:
            self.GAN.ema()

        if self.GAN.steps <= 25000 and self.GAN.steps % 1000 == 2:
            self.GAN.ma_init()

        if np.isnan(a):
            print("NaN Value Error.")
            exit()

        # Print info
        if self.GAN.steps % 100 == 0 and not self.silent:
            print("\n\nRound " + str(self.GAN.steps) + ":")
            print("D:", np.array(a))
            print("G:", np.array(b))
            print("PL:", self.pl_mean)

            s = round((time.clock() - self.lastblip), 4)
            self.lastblip = time.clock()

            steps_per_second = 100 / s
            steps_per_minute = steps_per_second * 60
            steps_per_hour = steps_per_minute * 60
            print("Steps/Second: " + str(round(steps_per_second, 2)))
            print("Steps/Hour: " + str(round(steps_per_hour)))

            min1k = floor(1000 / steps_per_minute)
            sec1k = floor(1000 / steps_per_second) % 60
            print("1k Steps: " + str(min1k) + ":" + str(sec1k))
            steps_left = 200000 - self.GAN.steps + 1e-7
            hours_left = steps_left // steps_per_hour
            minutes_left = (steps_left // steps_per_minute) % 60

            print("Til Completion: " + str(int(hours_left)) + "h" + str(int(minutes_left)) + "m")
            print()

            # Save Model
            if self.GAN.steps % 500 == 0:
                self.save(floor(self.GAN.steps / 10000))
            if self.GAN.steps % 1000 == 0 or (self.GAN.steps % 100 == 0 and self.GAN.steps < 2500):
                self.evaluate(floor(self.GAN.steps / 1000))

        printProgressBar(self.GAN.steps % 100, 99, decimals=0)

        self.GAN.steps = self.GAN.steps + 1

    @tf.function
    def train_step(self, images, style, noise, perform_gp=True, perform_pl=False):

        with tf.GradientTape() as gen_tape, tf.GradientTape() as disc_tape:
            # Get style information
            w_space = []
            pl_lengths = self.pl_mean
            for i in range(len(style)):
                w_space.append(self.GAN.S(style[i]))

            # Generate images
            generated_images = self.GAN.G(w_space + [noise])

            # Discriminate
            real_output = self.GAN.D(images, training=True)
            fake_output = self.GAN.D(generated_images, training=True)

            # Hinge loss function
            gen_loss = K.mean(fake_output)
            divergence = K.mean(K.relu(1 + real_output) + K.relu(1 - fake_output))
            disc_loss = divergence

            if perform_gp:
                # R1 gradient penalty
                disc_loss += gradient_penalty(images, real_output, 10)

            if perform_pl:
                # Slightly adjust W space
                w_space_2 = []
                for i in range(len(style)):
                    std = 0.1 / (K.std(w_space[i], axis=0, keepdims=True) + 1e-8)
                    w_space_2.append(w_space[i] + K.random_normal(tf.shape(w_space[i])) / (std + 1e-8))

                # Generate from slightly adjusted W space
                pl_images = self.GAN.G(w_space_2 + [noise])

                # Get distance after adjustment (path length)
                delta_g = K.mean(K.square(pl_images - generated_images), axis=[1, 2, 3])
                pl_lengths = delta_g

                if self.pl_mean > 0:
                    gen_loss += K.mean(K.square(pl_lengths - self.pl_mean))

        # Get gradients for respective areas
        gradients_of_generator = gen_tape.gradient(gen_loss, self.GAN.GM.trainable_variables)
        gradients_of_discriminator = disc_tape.gradient(disc_loss, self.GAN.D.trainable_variables)

        # Apply gradients
        self.GAN.GMO.apply_gradients(zip(gradients_of_generator, self.GAN.GM.trainable_variables))
        self.GAN.DMO.apply_gradients(zip(gradients_of_discriminator, self.GAN.D.trainable_variables))

        return disc_loss, gen_loss, divergence, pl_lengths

    def evaluate(self, num=0):

        n1 = self.noise_list(64)
        n2 = self.n_image(64)

        generated_images = self.GAN.GM.predict(n1 + [n2], batch_size=self.batch_size)

        r = []
        for i in range(0, 64, 8): r.append(np.concatenate(generated_images[i:i + 8], axis=1))

        c1 = np.concatenate(r, axis=0)
        c1 = np.clip(c1, 0.0, 1.0)
        x = Image.fromarray(np.uint8(c1 * 255))

        x.save(self.results_path / f'i{str(num)}.png')

        # Moving Average

        generated_images = self.GAN.GMA.predict(n1 + [n2], batch_size=self.batch_size)

        r = []
        for i in range(0, 64, 8): r.append(np.concatenate(generated_images[i:i + 8], axis=1))

        c1 = np.concatenate(r, axis=0)
        c1 = np.clip(c1, 0.0, 1.0)

        x = Image.fromarray(np.uint8(c1 * 255))

        x.save(self.results_path / f'i{str(num)}-ema.png')

        # Mixing Regularities
        nn = self.noise(8)
        n1 = np.tile(nn, (8, 1))
        n2 = np.repeat(nn, 8, axis=0)
        tt = int(self.n_layers / 2)

        p1 = [n1] * tt
        p2 = [n2] * (self.n_layers - tt)

        latent = p1 + [] + p2

        generated_images = self.GAN.GMA.predict(latent + [self.n_image(64)], batch_size=self.batch_size)

        r = []
        for i in range(0, 64, 8): r.append(np.concatenate(generated_images[i:i + 8], axis=0))

        c1 = np.concatenate(r, axis=1)
        c1 = np.clip(c1, 0.0, 1.0)

        x = Image.fromarray(np.uint8(c1 * 255))

        x.save(self.results_path / f'i{str(num)}-mr.png')

    def generate_truncated(self, style, noi=np.zeros([44]), trunc=0.5, outImage=False, num=0):

        # Get W's center of mass
        if self.av.shape[0] == 44:  # 44 is an arbitrary value
            print("Approximating W center of mass")
            self.av = np.mean(self.GAN.S.predict(self.noise(2000), batch_size=64), axis=0)
            self.av = np.expand_dims(self.av, axis=0)

        if noi.shape[0] == 44:
            noi = self.n_image(64)

        w_space = []
        for i in range(len(style)):
            tempStyle = self.GAN.S.predict(style[i])
            tempStyle = trunc * (tempStyle - self.av) + self.av
            w_space.append(tempStyle)

        generated_images = self.GAN.GE.predict(w_space + [noi], batch_size=self.batch_size)

        if outImage:
            r = []

            for i in range(0, 64, 8):
                r.append(np.concatenate(generated_images[i:i + 8], axis=0))

            c1 = np.concatenate(r, axis=1)
            c1 = np.clip(c1, 0.0, 1.0)

            x = Image.fromarray(np.uint8(c1 * 255))

            x.save(self.results_path / f't{str(num)}.png')

        return generated_images

    def save_model(self, model, name, num):
        json = model.to_json()
        with open(self.models_path / f'{name}.json', "w") as json_file:
            json_file.write(json)

        model.save_weights(self.models_path / f'{name}_{str(num)}.h5')

    def load_model(self, name, num):

        file = open(self.models_path / f'{name}.json', 'r')
        json = file.read()
        file.close()

        mod = model_from_json(json, custom_objects={'Conv2DMod': Conv2DMod})
        mod.load_weights(self.models_path / f'{name}_{str(num)}.h5')

        return mod

    def save(self, num):  # Save JSON and Weights into /Models/
        self.save_model(self.GAN.S, "sty", num)
        self.save_model(self.GAN.G, "gen", num)
        self.save_model(self.GAN.D, "dis", num)

        self.save_model(self.GAN.GE, "genMA", num)
        self.save_model(self.GAN.SE, "styMA", num)

    def load(self, num):  # Load JSON and Weights from /Models/
        # Load Models
        self.GAN.D = self.load_model("dis", num)
        self.GAN.S = self.load_model("sty", num)
        self.GAN.G = self.load_model("gen", num)

        self.GAN.GE = self.load_model("genMA", num)
        self.GAN.SE = self.load_model("styMA", num)

        self.GAN.gen_model()
        self.GAN.gen_model_a()


if __name__ == "__main__":
    model = StyleGAN(directory='mars', save_directory='save', lr=0.0001, silent=False, latent_size=512, img_size=128)
    model.GAN.steps = 1

    while model.GAN.steps < 1000001:
        model.train()
