# -*- coding: utf-8 -*-
import pandas as pd
import numpy as np
import datetime
import xgboost as xgb
import tool
import warnings
import os
import gc
import sys
warnings.filterwarnings('ignore')

path = './data'
var_col = ["var" + str(i).zfill(3) for i in range(1, 69)]
category_col = ["var016", "var020", "var047", "var053", "var066"]
number_col = [i for i in var_col if i not in category_col]
enum_col = ["var016", "var020", "var047"]
bool_col = ["var053", "var066"]
head_col = ["ts", "wtid"]
ext_enum_col = ["var010", "var013", "var019", "var023", "var032", "var039", "var041", "var048", "var049", "var050",
                "var054", "var058", "var059", "var063", "var064"]  # 用作分类效果更好
large_limit_col = {"var019": 100, "var041": 10000, "var048": 100, "var059": 100}  # 需要更大的number_limit
threshold = 0.005
argv = sys.argv
if len(argv) < 2:
    os.environ['CUDA_VISIBLE_DEVICES'] = '0'
else:
    os.environ['CUDA_VISIBLE_DEVICES'] = str(argv[1])


def _model_predict(all_feature, predict_feature, predict_col, num_boost_round=1000):
    # 多余的col
    del_cols = None
    for index, i in enumerate(tool.types):
        if predict_col in i:
            del_cols = i.copy()
            break
    test_label_col = str(index) + "_test"
    del_cols.extend(["0_test", "1_test", "2_test", "3_test"])

    k_v = {}
    if predict_col in enum_col or predict_col in ext_enum_col:
        # 删除数量较少的类别
        def func_count(df):
            df['value_count'] = df[predict_col].count()
            return df
        if predict_col in large_limit_col.keys():
            number_limit = large_limit_col[predict_col]
        else:
            number_limit = 10
        all_feature = all_feature.groupby(predict_col).apply(func_count)
        del_test_size = len(all_feature[(all_feature[test_label_col] == 1) & (all_feature["value_count"] < number_limit)])
        print(predict_col, "del_test_size:", del_test_size)

        # 原本应有的所有测试集
        test_feature_org = all_feature[all_feature[test_label_col] == 1]
        test_feature_org.drop(["value_count"], axis=1, inplace=True)
        test_y_org = np.array(test_feature_org[predict_col])
        test_x_org = np.array(test_feature_org.drop(del_cols, axis=1))
        print("test_x_org", test_x_org.shape)

        all_feature = all_feature[all_feature["value_count"] >= number_limit]
        all_feature.drop(["value_count"], axis=1, inplace=True)

        # 将value转换为class
        label = all_feature[predict_col]
        all_y = sorted(list(set(label)))

        if len(all_y) == 1:
            # 只有一个值，直接返回预测结果
            print("only one value!")
            return np.array([all_y[0]] * len(predict_feature)), 1

        v_k = {}
        for k, v in enumerate(all_y):
            v_k[v] = k
            k_v[k] = v
        label = np.array([v_k[i] for i in label])
        all_feature[predict_col] = label

    train_feature = all_feature[all_feature[test_label_col] == 0]
    train_y = np.array(train_feature[predict_col])
    train_x = np.array(train_feature.drop(del_cols, axis=1))
    test_feature = all_feature[all_feature[test_label_col] == 1]
    test_y = np.array(test_feature[predict_col])
    test_x = np.array(test_feature.drop(del_cols, axis=1))
    predict_x = np.array(predict_feature.drop(del_cols, axis=1))
    print("train_x:", train_x.shape, "test_x:", test_x.shape, "predict_x", predict_x.shape)

    params = {'booster': 'gbtree',
              'eta': 0.02,
              'max_depth': 8,  # 5 4 3
              'colsample_bytree': 0.9,  # 0.8 0.7
              'subsample': 0.8,
              'min_child_weight': 40,  # 2 3
              'silent': 1,
              'nthread': 4,
              'tree_method': 'gpu_hist',
              "gpu_id": 0,
              "seed": 0
              }
    if predict_col in bool_col:
        params["objective"] = "binary:logistic"
        params["eval_metric"] = "error"
        params["is_unbalance"] = True
        eval_metric = None
    elif predict_col in enum_col or predict_col in ext_enum_col:
        params["objective"] = "multi:softmax"
        params["eval_metric"] = "merror"
        params["num_class"] = max(label) + 1
        eval_metric = None
    else:
        params["objective"] = "reg:linear"
        eval_metric = tool.xgb_metric

    train_set = xgb.DMatrix(train_x, label=train_y)
    valid_set = xgb.DMatrix(test_x, label=test_y)
    temp_model = xgb.train(params, train_set, num_boost_round=num_boost_round, evals=[(valid_set, "validate")],
                           feval=eval_metric, maximize=True, early_stopping_rounds=200, verbose_eval=False)
    test_pred = temp_model.predict(valid_set)

    # 把概率转换为label
    if predict_col in bool_col:
        test_pred = np.where(test_pred > 0.5, 1, 0)
    elif predict_col in enum_col or predict_col in ext_enum_col:
        # 用原始的全测试集
        if del_test_size > 0:
            valid_set = xgb.DMatrix(test_x_org)
            test_pred = temp_model.predict(valid_set)
        test_y = test_y_org
        # 取回原来的值
        test_pred = np.array([k_v[i] for i in test_pred])

    if predict_col in category_col:
        test_s = tool.label_score(test_y, test_pred)
    else:
        test_s = tool.regression_score(test_y, test_pred)

    # 可能保留两位小数或一位小数更好
    if_round = False
    test_pred2 = np.round(test_pred, 2)
    test_s2 = tool.regression_score(test_y, test_pred2)
    if test_s < test_s2 - threshold:
        if_round = 2
        test_s = test_s2
    test_pred2 = np.round(test_pred, 1)
    test_s2 = tool.regression_score(test_y, test_pred2)
    if test_s < test_s2 - threshold:
        if_round = 1
        test_s = test_s2
    test_pred2 = np.round(test_pred, 0)
    test_s2 = tool.regression_score(test_y, test_pred2)
    if test_s < test_s2 - threshold:
        if_round = 0
        test_s = test_s2

    print("best iteration: ", temp_model.best_iteration)
    print("test score: ", test_s)

    predict_set = xgb.DMatrix(predict_x)
    predict_target = temp_model.predict(predict_set)
    predict_target = np.array(predict_target)
    if predict_col in enum_col or predict_col in ext_enum_col:
        predict_target = np.array([k_v[i] for i in predict_target])
    elif predict_col in bool_col:
        predict_target = np.where(predict_target > 0.5, 1, 0)

    if if_round:
        predict_target = np.round(predict_target, if_round)

    return predict_target, test_s


if __name__ == "__main__":
    data = pd.read_hdf(path + '/train_test.h5')
    data["time"] = pd.to_datetime(data["ts"])
    data["day"] = data["time"].map(lambda x: x.day)
    data["hour"] = data["time"].map(lambda x: x.hour)
    data["minute"] = data["time"].map(lambda x: x.minute)
    data["weekday"] = data["time"].map(lambda x: x.weekday())
    data.drop(["time"], axis=1, inplace=True)

    score_df = pd.DataFrame()
    score_df["var"] = var_col
    final_result = data[data["count_miss"] > 0]
    start = datetime.datetime.now()

    for wtid in range(1, 34):
        print("============ wtid=", wtid, "============")
        use_data = data[data["wtid"] == wtid]
        test_scores = []

        for var in var_col:
            print("------------ var=", var, "------------")
            train_data = use_data[pd.notna(use_data[var])]
            predict_data = use_data[pd.isna(use_data[var])]

            train_data.drop(["ts", "wtid", "count_miss"], axis=1, inplace=True)
            predict_data.drop(["ts", "wtid", "count_miss"], axis=1, inplace=True)
            gc.collect()

            predict_y, test_score = _model_predict(train_data, predict_data, var, num_boost_round=3000)
            test_scores.append(test_score)
            final_result.loc[predict_data.index, var] = predict_y

        score_df[str(wtid)] = test_scores
        end = datetime.datetime.now()
        print("Used time: ", end-start, "\n")
        del use_data, train_data
        gc.collect()

    head_col.extend(var_col)
    final_result = final_result[head_col]
    final_result.sort_values(["wtid", "ts"], inplace=True)
    final_result.to_csv("./result/xgb_horizontal_result.csv", encoding="utf8", index=False, float_format='%.2f')

    score_df.set_index("var", inplace=True)
    score_df = score_df.T
    score_df.reset_index(inplace=True)
    score_df.rename(columns={"index": "wtid"}, inplace=True)
    score_df.to_csv("./result/xgb_horizontal_score.csv", encoding="utf8", index=False, float_format='%.4f')
