from osgeo import gdal, ogr, gdalnumeric
from osgeo.gdalconst import GA_ReadOnly
import pandas as pd
import geopandas as gpd
import numpy as np
from shapely import geometry
from shapely.geometry import Point
import shapefile as shp
import pysal as ps
import geopy
import sys
import datetime
import inspect
import os

gdal.PushErrorHandler('CPLQuietErrorHandler')


def clean_load_data(paths, param, countries):
    """

    :param paths:
    :param param:
    :param countries:
    :return:
    """
    timecheck('Start')
    # avoid reading the excel file each time(to be removed)
    if not os.path.isfile('savetimeseries_temp.hdf'):
        # Get dataframe with timeseries
        print('reading excel file (might take a bit of time)\n')
        df_raw = pd.read_excel(paths["load_ts"], header=0, skiprows=[0, 1, 2], sep=',', decimal='.')
        print('done')
        # Filter by year
        df_year = df_raw.loc[df_raw['Year'] == param["year"]]
        df_year.to_hdf('savetimeseries_temp.hdf', 'df')
    else:
        df_year = pd.read_hdf('savetimeseries_temp.hdf', 'df')

    # Scale based on coverage ratio
    df_scaled = df_year.copy()
    a = df_year.iloc[:, 5:].values
    b = df_year.iloc[:, 4].values
    c = a / b[:, np.newaxis] * 100
    df_scaled.iloc[:, 5:] = c
    del a, b, c

    # Reshape so that rows correspond to hours and columns to countries
    data = np.reshape(df_scaled.iloc[:, 5:].values.T, (-1, len(df_scaled['Country'].unique())), order='F')
    # Create dataframe where rows correspond to hours and columns to countries
    df_reshaped = pd.DataFrame(data, index=np.arange(data.shape[0]), columns=df_scaled['Country'].unique())

    # Rename countries
    df_renamed = df_reshaped.T.rename(index=param["load"]["dict_countries"])
    df_renamed = df_renamed.reset_index().rename(columns={'index': 'Country'})
    df_renamed = df_renamed.groupby(['Country']).sum()
    df_renamed.reset_index(inplace=True)

    # Reshape Renamed_df
    df_reshaped_renamed = pd.DataFrame(df_renamed.loc[:, df_renamed.columns != 'Country'].T.to_numpy(),
                                       columns=df_renamed['Country'])

    # Create time series for missing countries
    df_completed = df_reshaped_renamed.copy()
    missing_countries = param["load"]["missing_countries"]
    replacement = param["load"]["replacement"]
    for i in missing_countries.keys():
        df_completed.loc[:, i] = df_completed.loc[:, replacement] / df_completed[replacement].sum() * missing_countries[
            i]

    # Select only countries needed

    df_filtered = df_completed[countries['Country'].unique()]

    # Fill missing data by values from the day before, adjusted based on the trend of the previous five hours
    df_filled = df_filtered.copy()
    for i, j in np.argwhere(np.isnan(df_filtered.values)):
        df_filled.iloc[i, j] = df_filled.iloc[i - 5:i, j].sum() / df_filled.iloc[i - 5 - 24:i - 24, j].sum() * \
                               df_filled.iloc[i - 24, j].sum()

    timecheck('End')
    return df_filled


def get_sectoral_profiles(paths, param):
    '''
    Read and store the load profile for each sector in the table 'profiles'.
    '''
    timecheck('Start')
    dict_daytype = param["load"]["dict_daytype"]
    dict_season = param["load"]["dict_season"]

    # Prepare the dataframe for the daily load:
    start = datetime.datetime(param["year"], 1, 1)
    end = datetime.datetime(param["year"], 12, 31)
    hours = [str(x) for x in list(range(0, 24))]
    time_series = pd.DataFrame(data=np.zeros((365, 27)), index=None, columns=['Date', 'Day', 'Season'] + hours)
    time_series['Date'] = pd.date_range(start, end)
    time_series['Day'] = [dict_daytype[time_series.loc[i, 'Date'].day_name()] for i in time_series.index]
    time_series['Season'] = [dict_season[time_series.loc[i, 'Date'].month] for i in time_series.index]

    # Residential load
    residential_profile_raw = pd.read_excel(paths["profiles"]["RES"], header=[3, 4], skipinitialspace=True)
    residential_profile_raw.rename(columns={'Übergangszeit': 'Spring/Fall', 'Sommer': 'Summer',
                                            'Werktag': 'Working day', 'Sonntag/Feiertag': 'Sunday',
                                            'Samstag': 'Saturday'}, inplace=True)
    residential_profile = time_series.copy()
    for i in residential_profile.index:
        residential_profile.loc[i, hours] = list(
            residential_profile_raw[(residential_profile.loc[i, 'Season'], residential_profile.loc[i, 'Day'])])
    # Reshape the hourly load in one vector, where the rows are the hours of the year
    residential_profile = np.reshape(residential_profile.loc[:, hours].values, -1, order='C')
    profiles = pd.DataFrame(residential_profile / residential_profile.sum(), columns=['RES'])

    # Industrial load
    if 'IND' in param["load"]["sectors"]:
        industrial_profile_raw = pd.read_excel(paths["profiles"]["IND"], header=0)
        industrial_profile_raw.rename(columns={'Stunde': 'Hour', 'Last': 'Load'}, inplace=True)
        # Reshape the hourly load in one vector, where the rows are the hours of the year
        industrial_profile = np.tile(industrial_profile_raw['Load'].values, 365)
        profiles['IND'] = industrial_profile / industrial_profile.sum()

    # Commercial load
    if 'COM' in param["load"]["sectors"]:
        commercial_profile_raw = pd.read_csv(paths["profiles"]["COM"], sep='[;]', engine='python', decimal=',',
                                             skiprows=[0, 99], header=[0, 1],
                                             skipinitialspace=True)
        # commercial_profile_raw.rename(columns={'Übergangszeit': 'Spring/Fall', 'Sommer': 'Summer',
        #                                        'Werktag': 'Working day', 'Sonntag': 'Sunday', 'Samstag': 'Saturday'},
        #                               inplace=True)
        commercial_profile_raw.rename(columns={'Ãœbergangszeit': 'Spring/Fall', 'Sommer': 'Summer',
                                               'Werktag': 'Working day', 'Sonntag': 'Sunday', 'Samstag': 'Saturday'},
                                      inplace=True)
        # Aggregate from 15 min --> hourly load
        commercial_profile_raw[('Hour', 'All')] = [int(str(commercial_profile_raw.loc[i, ('G0', '[W]')])[:2]) for i in
                                                   commercial_profile_raw.index]
        commercial_profile_raw = commercial_profile_raw.groupby([('Hour', 'All')]).sum()
        commercial_profile_raw.reset_index(inplace=True)
        commercial_profile = time_series.copy()
        for i in commercial_profile.index:
            commercial_profile.loc[i, hours] = list(
                commercial_profile_raw[(commercial_profile.loc[i, 'Season'], commercial_profile.loc[i, 'Day'])])
        # Reshape the hourly load in one vector, where the rows are the hours of the year
        commercial_profile = np.reshape(commercial_profile.loc[:, hours].values, -1, order='C')
        profiles['COM'] = commercial_profile / commercial_profile.sum()

    # Agricultural load
    if 'AGR' in param["load"]["sectors"]:
        agricultural_profile_raw = pd.read_csv(paths["profiles"]["AGR"], sep='[;]', engine='python', decimal=',',
                                               skiprows=[0, 99], header=[0, 1],
                                               skipinitialspace=True)
        # agricultural_profile_raw.rename(columns={'Übergangszeit': 'Spring/Fall', 'Sommer': 'Summer',
        #                                         'Werktag': 'Working day', 'Sonntag': 'Sunday', 'Samstag': 'Saturday'},
        #                                 inplace=True)
        agricultural_profile_raw.rename(columns={'Ãœbergangszeit': 'Spring/Fall', 'Sommer': 'Summer',
                                                 'Werktag': 'Working day', 'Sonntag': 'Sunday', 'Samstag': 'Saturday'},
                                        inplace=True)
        # Aggregate from 15 min --> hourly load
        agricultural_profile_raw['Hour'] = [int(str(agricultural_profile_raw.loc[i, ('L0', '[W]')])[:2]) for i in
                                            agricultural_profile_raw.index]
        agricultural_profile_raw = agricultural_profile_raw.groupby(['Hour']).sum()
        agricultural_profile = time_series.copy()
        for i in agricultural_profile.index:
            agricultural_profile.loc[i, hours] = list(
                agricultural_profile_raw[(agricultural_profile.loc[i, 'Season'], agricultural_profile.loc[i, 'Day'])])
        # Reshape the hourly load in one vector, where the rows are the hours of the year
        agricultural_profile = np.reshape(agricultural_profile.loc[:, hours].values, -1, order='C')
        profiles['AGR'] = agricultural_profile / agricultural_profile.sum()

    # Street lights
    if 'STR' in param["load"]["sectors"]:
        streets_profile_raw = pd.read_excel(paths["profiles"]["STR"], header=[4], skipinitialspace=True,
                                            usecols=[0, 1, 2])
        # Aggregate from 15 min --> hourly load
        streets_profile_raw['Hour'] = [int(str(streets_profile_raw.loc[i, 'Uhrzeit'])[:2]) for i in
                                       streets_profile_raw.index]
        streets_profile_raw = streets_profile_raw.groupby(['Datum', 'Hour']).sum()
        streets_profile_raw.iloc[0] = streets_profile_raw.iloc[0] + streets_profile_raw.iloc[-1]
        streets_profile_raw = streets_profile_raw.iloc[:-1]
        # Reshape the hourly load in one vector, where the rows are the hours of the year
        streets_profile = streets_profile_raw.values
        # Normalize the load over the year, ei. integral over the year of all loads for each individual sector is 1
        profiles['STR'] = streets_profile / streets_profile.sum()

    timecheck('End')
    return profiles


def intersection_regions_countries(paths):
    '''
    description
    '''

    # load shapefiles, and create spatial indexes for both files
    border_region = gpd.GeoDataFrame.from_file(paths["SHP"])
    border_region['geometry'] = border_region.buffer(0)
    border_country = gpd.GeoDataFrame.from_file(paths["Countries"])
    data = []
    for index, region in border_region.iterrows():
        for index2, country in border_country.iterrows():
            if (region.Population > 0):
                if region['geometry'].intersects(country['geometry']):
                    data.append({'geometry': region['geometry'].intersection(country['geometry']),
                                 'NAME_SHORT': region['NAME_SHORT'] + '_' + country['NAME_SHORT']})

    # Clean data
    i = 0
    list_length = len(data)
    while i < list_length:
        if data[i]['geometry'].geom_type == 'Polygon':
            data[i]['geometry'] = geometry.multipolygon.MultiPolygon([data[i]['geometry']])
        if not (data[i]['geometry'].geom_type == 'Polygon' or data[i]['geometry'].geom_type == 'MultiPolygon'):
            del data[i]
            list_length = list_length - 1
        else:
            i = i + 1

    # Create GeoDataFrame
    intersection = gpd.GeoDataFrame(data, columns=['geometry', 'NAME_SHORT'])
    intersection.to_file(paths["model_regions"] + 'intersection.shp')


def bbox_to_pixel_offsets(gt, bbox):
    originX = gt[0]
    originY = gt[3]
    pixel_width = gt[1]
    pixel_height = gt[5]
    x1 = int((bbox[0] - originX) / pixel_width)
    x2 = int((bbox[1] - originX) / pixel_width) + 1

    y1 = int((bbox[3] - originY) / pixel_height)
    y2 = int((bbox[2] - originY) / pixel_height) + 1

    xsize = x2 - x1
    ysize = y2 - y1

    return x1, y1, xsize, ysize


def zonal_stats(vector_path, raster_path, raster_type, nodata_value=None, global_src_extent=False):
    """
    Zonal Statistics
    Vector-Raster Analysis
    
    Copyright 2013 Matthew Perry
    
    Usage:
      zonal_stats.py VECTOR RASTER
      zonal_stats.py -h | --help
      zonal_stats.py --version
    
    Options:
      -h --help     Show this screen.
      --version     Show version.
    """

    rds = gdal.Open(raster_path, GA_ReadOnly)
    assert (rds)
    rb = rds.GetRasterBand(1)
    rgt = rds.GetGeoTransform()

    if nodata_value:
        nodata_value = float(nodata_value)
        rb.SetNoDataValue(nodata_value)

    vds = ogr.Open(vector_path, GA_ReadOnly)  # TODO maybe open update if we want to write stats
    assert (vds)
    vlyr = vds.GetLayer(0)

    # create an in-memory numpy array of the source raster data
    # covering the whole extent of the vector layer
    if global_src_extent:
        # use global source extent
        # useful only when disk IO or raster scanning inefficiencies are your limiting factor
        # advantage: reads raster data in one pass
        # disadvantage: large vector extents may have big memory requirements
        src_offset = bbox_to_pixel_offsets(rgt, vlyr.GetExtent())
        src_array = rb.ReadAsArray(*src_offset)

        # calculate new geotransform of the layer subset
        new_gt = (
            (rgt[0] + (src_offset[0] * rgt[1])),
            rgt[1],
            0.0,
            (rgt[3] + (src_offset[1] * rgt[5])),
            0.0,
            rgt[5]
        )

    mem_drv = ogr.GetDriverByName('Memory')
    driver = gdal.GetDriverByName('MEM')

    # Loop through vectors
    stats = []
    feat = vlyr.GetNextFeature()
    while feat is not None:

        if not global_src_extent:
            # use local source extent
            # fastest option when you have fast disks and well indexed raster (ie tiled Geotiff)
            # advantage: each feature uses the smallest raster chunk
            # disadvantage: lots of reads on the source raster
            src_offset = bbox_to_pixel_offsets(rgt, feat.geometry().GetEnvelope())
            src_array = rb.ReadAsArray(*src_offset)

            # calculate new geotransform of the feature subset
            new_gt = (
                (rgt[0] + (src_offset[0] * rgt[1])),
                rgt[1],
                0.0,
                (rgt[3] + (src_offset[1] * rgt[5])),
                0.0,
                rgt[5]
            )

        # Create a temporary vector layer in memory
        mem_ds = mem_drv.CreateDataSource('out')
        mem_layer = mem_ds.CreateLayer('poly', None, ogr.wkbPolygon)
        mem_layer.CreateFeature(feat.Clone())

        # Rasterize it
        rvds = driver.Create('', src_offset[2], src_offset[3], 1, gdal.GDT_Byte)
        rvds.SetGeoTransform(new_gt)
        gdal.RasterizeLayer(rvds, [1], mem_layer, burn_values=[1])
        rv_array = rvds.ReadAsArray()

        # Mask the source data array with our current feature
        # we take the logical_not to flip 0<->1 to get the correct mask effect
        # we also mask out nodata values explicitly
        masked = np.ma.MaskedArray(
            src_array,
            mask=np.logical_or(
                src_array == nodata_value,
                np.logical_not(rv_array)
            )
        )

        if raster_type == 'landuse':
            unique, counts = np.unique(masked, return_counts=True)
            unique2 = [str(i) for i in unique.astype(int)]
            count = dict(zip(unique2, counts.astype(int)))
            feature_stats = {
                # 'sum': float(masked.sum()),
                'NAME_SHORT': str(feat.GetField('NAME_SHORT'))}
            feature_stats.update(count)
        elif raster_type == 'population':
            feature_stats = {
                # 'max': float(masked.max()),
                'sum': float(masked.sum()),
                # 'count': int(masked.count()),
                # 'fid': int(feat.GetFID()),
                'NAME_SHORT': str(feat.GetField('NAME_SHORT'))}
        elif raster_type == 'renewable':
            feature_stats = {
                'max': float(masked.max()),
                # 'sum': float(masked.sum()),
                # 'count': int(masked.count()),
                # 'fid': int(feat.GetFID()),
                'NAME_SHORT': str(feat.GetField('NAME_SHORT'))}

        stats.append(feature_stats)

        rvds = None
        mem_ds = None
        feat = vlyr.GetNextFeature()

    vds = None
    rds = None

    return stats


def zonal_weighting(paths, df_load, df_stat, s):
    shp_path = paths["Countries"]
    raster_path = paths["LU"]
    shp = ogr.Open(shp_path, 1)
    raster = gdal.Open(raster_path)
    lyr = shp.GetLayer()

    # Create memory target raster
    target_ds = gdal.GetDriverByName('GTiff').Create(paths["load"] + 'Europe_' + s + '_load_pax.tif',
                                                     raster.RasterXSize,
                                                     raster.RasterYSize,
                                                     1, gdal.GDT_Float32)
    target_ds.SetGeoTransform(raster.GetGeoTransform())
    target_ds.SetProjection(raster.GetProjection())

    # NoData value
    mem_band = target_ds.GetRasterBand(1)
    mem_band.Fill(0)
    mem_band.SetNoDataValue(0)

    # Add a new field
    if not field_exists('Weight_' + s, shp_path):
        new_field = ogr.FieldDefn('Weight_' + s, ogr.OFTReal)
        lyr.CreateField(new_field)

    for feat in lyr:
        country = feat.GetField('NAME_SHORT')[:2]
        if s == 'RES':
            feat.SetField('Weight_' + s, df_load[country, s] / df_stat.loc[country, 'RES'])
        else:
            feat.SetField('Weight_' + s, df_load[country, s] / df_stat.loc[country, s])
        lyr.SetFeature(feat)
        feat = None

    # Rasterize zone polygon to raster
    gdal.RasterizeLayer(target_ds, [1], lyr, None, None, [0], ['ALL_TOUCHED=FALSE', 'ATTRIBUTE=Weight_' + s[:3]])


def field_exists(field_name, shp_path):
    shp = ogr.Open(shp_path, 0)
    lyr = shp.GetLayer()
    lyr_dfn = lyr.GetLayerDefn()

    exists = False
    for i in range(lyr_dfn.GetFieldCount()):
        exists = exists or (field_name == lyr_dfn.GetFieldDefn(i).GetName())

    return exists


# 05a_Distribution_Renewable_powerplants

# ## Functions:

# https://pcjericks.github.io/py-gdalogr-cookbook/raster_layers.html#clip-a-geotiff-with-shapefile

def world2Pixel(geoMatrix, x, y):
    """
    Uses a gdal geomatrix (gdal.GetGeoTransform()) to calculate
    the pixel location of a geospatial coordinate
    """
    ulX = geoMatrix[0]
    ulY = geoMatrix[3]
    xDist = geoMatrix[1]
    yDist = geoMatrix[5]
    rtnX = geoMatrix[2]
    rtnY = geoMatrix[4]
    pixel = int((x - ulX) / xDist)
    line = int((ulY - y) / xDist)
    return (pixel, line)


def rasclip(raster_path, shapefile_path, counter):
    # Load the source data as a gdalnumeric array
    srcArray = gdalnumeric.LoadFile(raster_path)

    # Also load as a gdal image to get geotransform
    # (world file) info
    srcImage = gdal.Open(raster_path)
    geoTrans = srcImage.GetGeoTransform()

    # Create an OGR layer from a boundary shapefile
    shapef = ogr.Open(shapefile_path)
    lyr = shapef.GetLayer(os.path.split(os.path.splitext(shapefile_path)[0])[1])

    # Filter based on FID
    lyr.SetAttributeFilter("FID = {}".format(counter))
    poly = lyr.GetNextFeature()

    # Convert the polygon extent to image pixel coordinates
    minX, maxX, minY, maxY = poly.GetGeometryRef().GetEnvelope()
    ulX, ulY = world2Pixel(geoTrans, minX, maxY)
    lrX, lrY = world2Pixel(geoTrans, maxX, minY)

    # Calculate the pixel size of the new image
    pxWidth = int(lrX - ulX)
    pxHeight = int(lrY - ulY)

    clip = srcArray[ulY:lrY, ulX:lrX]

    # Create pixel offset to pass to new image Projection info
    xoffset = ulX
    yoffset = ulY
    # print("Xoffset, Yoffset = ( %f, %f )" % ( xoffset, yoffset ))

    # Create a second (modified) layer
    outdriver = ogr.GetDriverByName('MEMORY')
    source = outdriver.CreateDataSource('memData')
    # outdriver = ogr.GetDriverByName('ESRI Shapefile')
    # source = outdriver.CreateDataSource(mypath+'00 Inputs/maps/dummy.shp')
    lyr2 = source.CopyLayer(lyr, 'dummy', ['OVERWRITE=YES'])
    featureDefn = lyr2.GetLayerDefn()
    # create a new ogr geometry
    geom = poly.GetGeometryRef().Buffer(-1 / 240)
    # write the new feature
    newFeature = ogr.Feature(featureDefn)
    newFeature.SetGeometryDirectly(geom)
    lyr2.CreateFeature(newFeature)
    # here you can place layer.SyncToDisk() if you want
    newFeature.Destroy()
    # lyr2 = source.CopyLayer(lyr,'dummy',['OVERWRITE=YES'])
    lyr2.ResetReading()
    poly_old = lyr2.GetNextFeature()
    lyr2.DeleteFeature(poly_old.GetFID())

    # Create memory target raster
    target_ds = gdal.GetDriverByName('MEM').Create('', srcImage.RasterXSize, srcImage.RasterYSize, 1, gdal.GDT_Byte)
    target_ds.SetGeoTransform(geoTrans)
    target_ds.SetProjection(srcImage.GetProjection())

    # Rasterize zone polygon to raster
    gdal.RasterizeLayer(target_ds, [1], lyr2, None, None, [1], ['ALL_TOUCHED=FALSE'])
    mask = target_ds.ReadAsArray()
    mask = mask[ulY:lrY, ulX:lrX]

    # Clip the image using the mask
    clip = np.multiply(clip, mask).astype(gdalnumeric.float64)
    return poly.GetField('NAME_SHORT'), xoffset, yoffset, clip


def map_power_plants(p, x, y, c, paths):
    outSHPfn = paths["map_power_plants"] + p + '.shp'

    # Create the output shapefile
    shpDriver = ogr.GetDriverByName("ESRI Shapefile")
    if os.path.exists(outSHPfn):
        shpDriver.DeleteDataSource(outSHPfn)
    outDataSource = shpDriver.CreateDataSource(outSHPfn)
    outLayer = outDataSource.CreateLayer(outSHPfn, geom_type=ogr.wkbPoint)

    # create point geometry
    point = ogr.Geometry(ogr.wkbPoint)
    # create a field
    idField = ogr.FieldDefn('CapacityMW', ogr.OFTReal)
    outLayer.CreateField(idField)
    # Create the feature
    featureDefn = outLayer.GetLayerDefn()

    # Set values
    for i in range(0, len(x)):
        point.AddPoint(x[i], y[i])
        outFeature = ogr.Feature(featureDefn)
        outFeature.SetGeometry(point)
        outFeature.SetField('CapacityMW', c[i])
        outLayer.CreateFeature(outFeature)
    outFeature = None


def map_grid_plants(x, y, paths):
    outSHPfn = paths["map_grid_plants"]

    # Create the output shapefile
    shpDriver = ogr.GetDriverByName("ESRI Shapefile")
    if os.path.exists(outSHPfn):
        shpDriver.DeleteDataSource(outSHPfn)
    outDataSource = shpDriver.CreateDataSource(outSHPfn)
    outLayer = outDataSource.CreateLayer(outSHPfn, geom_type=ogr.wkbPoint)

    # create point geometry
    point = ogr.Geometry(ogr.wkbPoint)
    # Create the feature
    featureDefn = outLayer.GetLayerDefn()

    # Set values
    for i in range(0, len(x)):
        point.AddPoint(x[i], y[i])
        outFeature = ogr.Feature(featureDefn)
        outFeature.SetGeometry(point)
        outLayer.CreateFeature(outFeature)
    outFeature = None


def timecheck(*args):
    if len(args) == 0:
        print(inspect.stack()[1].function + str(datetime.datetime.now().strftime(": %H:%M:%S:%f")))

    elif len(args) == 1:
        print(inspect.stack()[1].function + ' - ' + str(args[0])
              + str(datetime.datetime.now().strftime(": %H:%M:%S:%f")))

    else:
        raise Exception('Too many arguments have been passed.\nExpected: zero or one \nPassed: ' + format(len(args)))


def display_progress(message, progress_stat):
    length = progress_stat[0]
    status = progress_stat[1]
    sys.stdout.write('\r')
    sys.stdout.write(message + ' ' + '[%-50s] %d%%' % ('=' * ((status * 50) // length), (status * 100) // length))
    sys.stdout.flush()
    if status == length:
        print('\n')


def crd_merra(Crd_regions, res_weather):
    ''' description '''
    Crd = np.array([(np.ceil((Crd_regions[:, 0] - res_weather[0] / 2) / res_weather[0])
                     * res_weather[0] + res_weather[0] / 2),
                    (np.ceil((Crd_regions[:, 1] - res_weather[1] / 2) / res_weather[1])
                     * res_weather[1] + res_weather[1] / 2),
                    (np.floor((Crd_regions[:, 2] + res_weather[0] / 2) / res_weather[0])
                     * res_weather[0] - res_weather[0] / 2),
                    (np.floor((Crd_regions[:, 3] + res_weather[1] / 2) / res_weather[1])
                     * res_weather[1] - res_weather[1] / 2)])
    Crd = Crd.T
    return Crd


def filter_life_time(param, raw, depreciation):
    if param["year"] > param["pro_sto"]["year_ref"]:
        # Set depreciation period
        for c in raw["CoIn"].unique():
            raw.loc[raw["CoIn"] == c, "lifetime"] = depreciation[c]
        lifetimeleft = raw["lifetime"] + raw["year"]
        current = raw.drop(raw.loc[lifetimeleft < param["year"]].index)
        print('Already depreciated processes:\n')
        print(str(len(raw) - len(current)) + '# process have been removed')
    else:
        current = raw.copy()
        print('Number of current processes: ' + str(len(current)))
    return current


def get_sites(current, paths):
    # Get regions from shapefile
    regions = gpd.read_file(paths["SHP"])
    regions["geometry"] = regions.buffer(0)

    # Spacial join
    current.crs = regions[["NAME_SHORT", "geometry"]].crs
    located = gpd.sjoin(current, regions[["NAME_SHORT", "geometry"]], how='left', op='intersects')
    located.rename(columns={'NAME_SHORT': 'Site'}, inplace=True)

    # Remove duplicates that lie in the border between land and sea
    located.drop_duplicates(subset=["CoIn", "Pro", "inst-cap", "year", "Site"], inplace=True)

    # Remove duplicates that lie in two different zones
    located = located.loc[~located.index.duplicated(keep='last')]

    located.dropna(axis=0, subset=["Site"], inplace=True)

    return located


def closest_polygon(geom, polygons):
    """Returns polygon from polygons that is closest to geom.

    Args:
        geom: shapely geometry (used here: a point)
        polygons: GeoDataFrame of non-overlapping (!) polygons

    Returns:
        The polygon from 'polygons' which is closest to 'geom'.
    """
    dist = np.inf
    for poly in polygons.index:
        if polygons.loc[poly].geometry.convex_hull.exterior.distance(geom) < dist:
            dist = polygons.loc[poly].geometry.convex_hull.exterior.distance(geom)
            closest = polygons.loc[poly]
    return closest


def containing_polygon(geom, polygons):
    """Returns polygon from polygons that contains geom.

    Args:
        geom: shapely geometry (used here: a point)
        polygons: GeoDataFrame of non-overlapping (!) polygons

    Returns:
        The polygon from 'polygons' which contains (in
        the way shapely implements it) 'geom'. Throws
        an error if more than one polygon contain 'geom'.
        Returns 'None' if no polygon contains it.
    """
    try:
        containing_polygons = polygons[polygons.contains(geom)]
    except:
        containing_polygons = []
    if len(containing_polygons) == 0:
        return closest_polygon(geom, polygons)
    if len(containing_polygons) > 1:
        print(containing_polygons)
        # raise ValueError('geom lies in more than one polygon!')
    return containing_polygons.iloc[0]


def reverse_lines(df):
    """Reverses the line direction if the starting point is alphabetically
    after the end point.

    Args:
        df: dataframe with columns 'Region_start' and 'Region_end'.

    Returns:
        The same dataframe after the line direction has been reversed.
    """
    for idx in df.index:
        if df.Region_start[idx] > df.Region_end[idx]:
            df.loc[idx, 'Region_start'], df.loc[idx, 'Region_end'] = df.loc[idx, 'Region_end'], df.loc[
                idx, 'Region_start']
    df_final = df
    return df_final


def string_to_int(mylist):
    """This function converts list entries from strings to integers.

    Args:
        mylist: list eventually containing some integers interpreted
        as string elements.

    Returns:
        The same list after the strings where converted to integers.
    """
    result = [int(i) for i in mylist]
    return result


def zero_free(mylist):
    """This function deletes zero entries from a list.

    Args:
        mylist: list eventually containing zero entries.

    Returns:
        The same list after the zero entries where removed.
    """
    result = []
    for j in np.arange(len(mylist)):
        if mylist[j] > 0:
            result = result + [mylist[j]]
    return result


def add_suffix(df, suffix):
    # Check whether there is only one copy of the initial row, or more
    if str(df.index_old.iloc[1]).find('_') > 0:  # There are more than one copy of the row
        # Increment the suffix and replace the old one
        suffix = suffix + 1
        df.index_old.iloc[1] = df.index_old.iloc[1].replace('_' + str(suffix - 1), '_' + str(suffix))
    else:  # No other copy has been created so far
        # Reinitialize the suffix and concatenate it at the end of the old index
        suffix = 1
        df.index_old.iloc[1] = str(df.index_old.iloc[1]) + '_' + str(suffix)
    return (df, suffix)


def deduplicate_lines(df):
    """ Aggregate bidirectional lines to single lines.

    Given a th"""
    # aggregate val of rows with (a,b,t) == (b,a,t)
    idx = 0
    while idx < len(df) - 1:
        if (df.iloc[idx, 0] == df.iloc[idx + 1, 0]) & (
                df.iloc[idx, 1] == df.iloc[idx + 1, 1]):  # & (df.iloc[idx,2] == df.iloc[idx+1,2]):
            df.iloc[idx, 26] = df.iloc[idx, 26] + df.iloc[idx + 1, 26]  # Capacity MVA
            df.iloc[idx, 23] = 1 / (1 / df.iloc[idx, 23] + 1 / df.iloc[idx + 1, 23])  # Specific resistance Ohm/km
            df.iloc[idx, 13] = df.iloc[idx, 13] + df.iloc[idx + 1, 13]  # Length
            df = df.drop(df.index[idx + 1])
        else:
            idx += 1

    df_final = df
    return df_final


def match_wire_voltages(grid_sorted):
    timecheck('Start')
    """
    the columns 'voltage' and 'wires' may contain multiple values separated with a semicolon. The goal is to assign
    a voltage to every circuit, whenever possible.

    Algorithm:

    [Case #1] If (n_voltages_count = 1), then every circuit is on that voltage level. We can replace the list
    entries in 'wires' with their sum;
    Else:
    [Case #2] If (n_circuits_count = n_voltages_count), then update the value in the list 'wires' so that each
    voltage level has only one circuit;
    [Case #3] If (n_circuits_count < n_voltages_count), then ignore the exceeding voltages and update the value in
    the list 'Circuits' so that each voltage level has only one circuit;
    [Case #4] If (n_voltages_count < n_circuits), then assign the highest voltage to the rest of the circuits;
    [Case #5] If (n_circuits < n_voltages_count) and (n_voltages_count < n_circuits_count), then ignore
    the exceeding voltages so that each voltage level has as many circuits as in the list entries of 'wires'.

    :param grid_sorted:
    :return:
    """

    n_circuits = pd.Series(map(string_to_int, grid_sorted.wires.str.split(';')))
    n_circuits_count = pd.Series(map(sum, n_circuits), index=grid_sorted.index)
    n_circuits = pd.Series(map(len, n_circuits), index=grid_sorted.index)
    n_voltages = pd.Series(map(zero_free, map(string_to_int, grid_sorted.voltage.str.split(';'))))
    n_voltages_count = pd.Series(map(len, n_voltages), index=grid_sorted.index)
    n_voltages = pd.Series(n_voltages, index=grid_sorted.index)
    grid_sorted.voltage = n_voltages

    # Case 1: (n_voltages_count = 1)
    ind_excerpt = grid_sorted[n_voltages_count == 1].index
    grid_clean = grid_sorted.loc[ind_excerpt]
    grid_dirty = grid_sorted.loc[grid_sorted[n_voltages_count != 1].index]
    n_circuits.loc[ind_excerpt] = n_circuits_count.loc[ind_excerpt]
    grid_clean.loc[:, 'wires'] = n_circuits.loc[ind_excerpt]

    # Reindex in order to avoid user warnings later
    n_circuits_count = n_circuits_count.reindex(grid_dirty.index)
    n_circuits = n_circuits.reindex(grid_dirty.index)
    n_voltages_count = n_voltages_count.reindex(grid_dirty.index)
    n_voltages = n_voltages.reindex(grid_dirty.index)

    # Case 2: (n_circuits_count = n_voltages_count)
    ind_excerpt = grid_dirty[n_circuits_count == n_voltages_count].index
    n_circuits.loc[ind_excerpt] = n_circuits_count.loc[ind_excerpt]
    grid_dirty.loc[ind_excerpt, 'wires'] = [';'.join(['1'] * n_circuits_count.loc[i]) for i in ind_excerpt]

    # Case 3: (n_circuits_count < n_voltages_count)
    ind_excerpt = grid_dirty[n_circuits_count < n_voltages_count].index
    n_circuits.loc[ind_excerpt] = n_circuits_count.loc[ind_excerpt]
    n_voltages_count.loc[ind_excerpt] = n_circuits.loc[ind_excerpt]
    n_voltages.loc[ind_excerpt] = [grid_dirty.loc[i, 'voltage'][:n_circuits_count.loc[i]] for i in ind_excerpt]
    grid_dirty.loc[ind_excerpt, 'voltage'] = n_voltages.loc[ind_excerpt]
    grid_dirty.loc[ind_excerpt, 'wires'] = [';'.join(['1'] * n_circuits_count.loc[i]) for i in ind_excerpt]

    # Case 4: (n_voltages_count < n_circuits)
    ind_excerpt = grid_dirty[(n_voltages_count < n_circuits) & (n_voltages_count > 0)].index
    missing_voltages = n_circuits.loc[ind_excerpt] - n_voltages_count.loc[ind_excerpt]
    n_voltages_count.loc[ind_excerpt] = n_circuits.loc[ind_excerpt]
    for i in ind_excerpt:
        for j in np.arange(missing_voltages[i]):
            n_voltages.loc[i].append(max(grid_dirty.loc[i, 'voltage']))
            grid_dirty.loc[i, 'voltage'].append(max(grid_dirty.loc[i, 'voltage']))

    # Case 5: (n_circuits < n_voltages_count) and (n_voltages_count < n_circuits_count)
    ind_excerpt = grid_dirty[(n_circuits < n_voltages_count) & (n_voltages_count < n_circuits_count)].index
    n_voltages_count.loc[ind_excerpt] = n_circuits.loc[ind_excerpt]
    n_voltages.loc[ind_excerpt] = [grid_dirty.loc[i, 'voltage'][:n_circuits.loc[i]] for i in ind_excerpt]
    grid_dirty.loc[ind_excerpt, 'voltage'] = n_voltages.loc[ind_excerpt]

    # By now n_circuits = n_voltages_count, so that we can split the list entries of 'voltage' and 'wires'
    # in exactly the same amount of rows:

    suffix = 1  # When we create a new row, we will add a suffix to the old index
    status = 0
    count = len(grid_dirty)
    while len(grid_dirty):
        status = count - len(grid_dirty) + 1
        display_progress("Cleaning GridKit progress: ", (count, status))
        # In case the first line is clean
        if grid_dirty.wires.iloc[0].count(';') == 0:
            grid_clean = grid_clean.append(grid_dirty.iloc[0], ignore_index=True)
            grid_dirty = grid_dirty.drop(grid_dirty.index[[0]])
        else:
            # Append a copy of the first row of grid_dirty at the top of the same dataframe
            grid_dirty = grid_dirty.iloc[0].to_frame().transpose().append(grid_dirty, ignore_index=True)
            # Extract the first number of circuits from that row and remove the rest of the string
            grid_dirty.wires.iloc[0] = grid_dirty.wires.iloc[0][:grid_dirty.wires.iloc[0].find(';')]
            # Extract the first voltage level from that row and remove the rest of the list
            grid_dirty.voltage.iloc[0] = grid_dirty.voltage.iloc[0][:1]

            # Add the right suffix
            grid_dirty, suffix = add_suffix(grid_dirty, suffix)

            # Update the string in the original row
            grid_dirty.wires.iloc[1] = grid_dirty.wires.iloc[1][grid_dirty.wires.iloc[1].find(';') + 1:]
            grid_dirty.voltage.iloc[1] = grid_dirty.voltage.iloc[1][1:]

            # Move the 'clean' row to grid_clean, and drop it from grid_dirty
            grid_clean = grid_clean.append(grid_dirty.iloc[0], ignore_index=True)
            grid_dirty = grid_dirty.drop(grid_dirty.index[[0]])

    # Express voltage in kV
    grid_clean.voltage = pd.Series([grid_clean.loc[i, 'voltage'][0] / 1000 for i in grid_clean.index],
                                   index=grid_clean.index)
    print(' \n')
    timecheck('End')
    return grid_clean


def set_loadability(grid_filled, param):
    loadability = param["grid"]["loadability"]
    grid_filled.loc[grid_filled[grid_filled.length_m <= float(80)].index, 'loadability_c'] = loadability["80"]
    grid_filled.loc[grid_filled[(grid_filled.length_m > 80) & (grid_filled.length_m <= 100)].index, 'loadability_c'] \
        = loadability["100"]
    grid_filled.loc[grid_filled[(grid_filled.length_m > 100) & (grid_filled.length_m <= 150)].index, 'loadability_c'] \
        = loadability["150"]
    grid_filled.loc[grid_filled[(grid_filled.length_m > 150) & (grid_filled.length_m <= 200)].index, 'loadability_c'] \
        = loadability["200"]
    grid_filled.loc[grid_filled[(grid_filled.length_m > 200) & (grid_filled.length_m <= 250)].index, 'loadability_c'] \
        = loadability["250"]
    grid_filled.loc[grid_filled[(grid_filled.length_m > 250) & (grid_filled.length_m <= 300)].index, 'loadability_c'] \
        = loadability["300"]
    grid_filled.loc[grid_filled[(grid_filled.length_m > 300) & (grid_filled.length_m <= 350)].index, 'loadability_c'] \
        = loadability["350"]
    grid_filled.loc[grid_filled[(grid_filled.length_m > 350) & (grid_filled.length_m <= 400)].index, 'loadability_c'] \
        = loadability["400"]
    grid_filled.loc[grid_filled[(grid_filled.length_m > 400) & (grid_filled.length_m <= 450)].index, 'loadability_c'] \
        = loadability["450"]
    grid_filled.loc[grid_filled[(grid_filled.length_m > 450) & (grid_filled.length_m <= 500)].index, 'loadability_c'] \
        = loadability["500"]
    grid_filled.loc[grid_filled[(grid_filled.length_m > 500) & (grid_filled.length_m <= 550)].index, 'loadability_c'] \
        = loadability["550"]
    grid_filled.loc[grid_filled[(grid_filled.length_m > 550) & (grid_filled.length_m <= 600)].index, 'loadability_c'] \
        = loadability["600"]
    grid_filled.loc[grid_filled[(grid_filled.length_m > 600) & (grid_filled.length_m <= 650)].index, 'loadability_c'] \
        = loadability["650"]
    grid_filled.loc[grid_filled[(grid_filled.length_m > 650) & (grid_filled.length_m <= 700)].index, 'loadability_c'] \
        = loadability["700"]
    grid_filled.loc[grid_filled[grid_filled["length_m"] > 700].index, 'loadability_c'] = loadability["750"]

    return grid_filled
